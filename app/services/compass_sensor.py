"""
Real device-heading (magnetic compass) support via Android's
``SensorManager``, accessed through pyjnius.

Everything sensor/JNI-related lives in this one module, on purpose --
nothing else in the app imports ``jnius`` directly. The rest of the app
only ever sees :class:`CompassSensorService`'s small public surface
(``start()``, ``diagnostics``, ``drain_latest_heading()``), so a change
here can never leak into the map, the parser, or the existing compass UI.

How this actually determines a heading (no faking, no GPS):
  1. Register Android ``SensorEventListener`` s (implemented directly in
     Python via ``jnius.PythonJavaClass`` -- no separate .java/.kt file
     needed) for ``Sensor.TYPE_MAGNETIC_FIELD`` and
     ``Sensor.TYPE_ACCELEROMETER``.
  2. Every time either sensor reports a new reading, recompute a
     tilt-compensated azimuth from the LATEST accelerometer +
     magnetometer vectors using the same rotation-matrix method
     Android's own ``SensorManager.getRotationMatrix``/``getOrientation``
     use internally (reimplemented in plain Python here -- see
     ``_tilt_compensated_azimuth_degrees`` -- specifically to avoid
     marshaling Java ``float[]`` arrays back and forth through JNI for
     the intermediate matrices, which is one of the more fragile parts
     of pyjnius interop; only the raw per-axis sensor readings cross the
     JNI boundary).
  3. The result is smoothed with a circular (wrap-safe) exponential
     moving average and pushed onto a small thread-safe queue.
  4. The app's existing 1-second tick loop (see main.py) drains that
     queue and forwards the latest heading to the existing
     ``CompassRadar``/``ThreatCompassView`` -- this module never touches
     the UI directly.

Threading note: ``onSensorChanged`` fires on an Android-owned thread, not
one Python created -- ``jnius.attach_thread()`` is called first thing
inside every callback (see ``_SensorCallback``) before touching any
Python/JNI state, per pyjnius's own documented requirement for this
exact situation.
"""

from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

#: Android sensor type constants (android.hardware.Sensor), inlined so
#: this module doesn't need a live JNI connection just to know their
#: values -- these are long-stable, publicly documented Android SDK
#: constants, not something that varies by device.
_TYPE_ACCELEROMETER = 1
_TYPE_MAGNETIC_FIELD = 2
_TYPE_GYROSCOPE = 4

#: SensorManager.SENSOR_DELAY_NORMAL -- the real Android SDK constant
#: values are FASTEST=0, GAME=1, UI=2, NORMAL=3 (a previous version of
#: this file used 1, mislabeled as "UI" -- that was actually GAME, a
#: faster rate than intended). Deliberately using the SLOWEST standard
#: rate here, not even the real UI rate: a compass display doesn't need
#: low latency (the exponential smoothing in _CircularSmoother already
#: assumes a steady stream, not a fast one), and fewer sensor events
#: means fewer calls into pyjnius/JNI per second -- less surface area
#: for any threading issue while this integration is still new.
_SENSOR_DELAY_NORMAL = 3

#: How much weight a brand-new reading gets in the smoothed heading
#: (0..1 -- higher follows the device faster but jitters more, lower is
#: smoother but lags). Tuned for "visibly responsive but not twitchy".
_SMOOTHING_ALPHA = 0.18


@dataclass(frozen=True, slots=True)
class CompassDiagnostics:
    """Real, current sensor status -- every field reflects an actual
    query against Android's SensorManager, never a guess.

    ``heading_degrees`` is only ever set from an actual computed,
    tilt-compensated azimuth (see module docstring); it is ``None``
    whenever a real reading isn't available yet or the platform can't
    provide one at all.
    """

    magnetometer_available: bool = False
    accelerometer_available: bool = False
    gyroscope_available: bool = False
    heading_degrees: Optional[float] = None

    @property
    def compass_functional(self) -> bool:
        """The two sensors an azimuth actually requires are both present.

        Gyroscope is reported for diagnostics only (per the
        requirement) -- it is not required for a real tilt-compensated
        compass and is not used in the heading math here.
        """
        return self.magnetometer_available and self.accelerometer_available


def _tilt_compensated_azimuth_degrees(
    accel: tuple[float, float, float], mag: tuple[float, float, float]
) -> Optional[float]:
    """Real tilt-compensated compass heading, in degrees (0=N, 90=E, ...).

    Same method Android's own ``SensorManager.getRotationMatrix`` +
    ``getOrientation`` use: build an orthonormal (East, North, Up) frame
    from the raw gravity + geomagnetic vectors (expressed in the
    device's own coordinate frame), then read the azimuth off it --
    specifically ``atan2(East.y, North.y)``, the Y (device "up"/top
    edge) components of the East and North basis vectors, which is what
    AOSP's ``getOrientation`` actually computes (not the more intuitive-
    looking but WRONG ``atan2(East.x, North.x)``).

    Reimplemented directly in Python (not called via JNI) so no Java
    float[] arrays need to cross the pyjnius boundary for this step --
    only the six raw per-axis floats already read out of the sensor
    events do.

    Returns ``None`` if the vectors are degenerate (e.g. the device is
    in free-fall or right next to a strong magnet) rather than dividing
    by zero or returning a meaningless heading.
    """
    ax, ay, az = accel
    mx, my, mz = mag

    norm_a = math.sqrt(ax * ax + ay * ay + az * az)
    if norm_a < 1e-6:
        return None
    ax, ay, az = ax / norm_a, ay / norm_a, az / norm_a

    # East (H) = geomagnetic x gravity, normalized.
    hx = my * az - mz * ay
    hy = mz * ax - mx * az
    hz = mx * ay - my * ax
    norm_h = math.sqrt(hx * hx + hy * hy + hz * hz)
    if norm_h < 1e-6:
        return None  # degenerate: magnetometer reading unusable right now
    hx, hy, hz = hx / norm_h, hy / norm_h, hz / norm_h

    # North (M) = gravity x East -- already unit length since gravity
    # and East are orthonormal. Only the Y component is needed below.
    my_component = az * hx - ax * hz

    azimuth_rad = math.atan2(hy, my_component)
    return math.degrees(azimuth_rad) % 360.0


class _CircularSmoother:
    """Exponential moving average that's safe across the 359 deg -> 0 deg
    wrap (plain EMA on raw degrees glitches there -- e.g. averaging 359
    and 1 naively gives 180, not 0).

    Implemented by smoothing the heading's own (cos, sin) unit vector
    instead of the angle directly, which has no wraparound to begin with.
    """

    def __init__(self, alpha: float) -> None:
        self._alpha = alpha
        self._x: Optional[float] = None
        self._y: Optional[float] = None

    def update(self, degrees_value: float) -> float:
        rad = math.radians(degrees_value)
        x, y = math.cos(rad), math.sin(rad)
        if self._x is None or self._y is None:
            self._x, self._y = x, y
        else:
            self._x += self._alpha * (x - self._x)
            self._y += self._alpha * (y - self._y)
        return math.degrees(math.atan2(self._y, self._x)) % 360.0


def _import_jnius():
    """Import jnius only if it's actually present.

    pyjnius is declared as an ANDROID-ONLY build dependency (see
    ``[tool.flet.android]`` in pyproject.toml) -- on desktop/web builds
    it is simply not installed, so this import fails cleanly with
    ``ImportError`` and every caller in this module treats that as "not
    on Android", never as an error to surface. This is the one and only
    platform gate in the whole module; nothing here otherwise assumes
    which platform it's running on.
    """
    try:
        import jnius  # type: ignore[import-not-found]

        return jnius
    except Exception:
        return None


def _resolve_android_context(jnius_module):
    """Find a usable Android ``Context`` across different Serious Python
    versions, without hardcoding a single assumed class name.

    Serious Python (Flet's Android embedding) has changed its activity
    class name across versions (seen in the wild: both
    ``com.flet.serious_python_android.PythonActivity`` and older/newer
    variants) -- rather than pin to one and silently break on a version
    mismatch, this tries each known-real strategy in turn and only
    returns ``None`` (-> "sensor unavailable") if none of them work.
    """
    autoclass = jnius_module.autoclass
    strategies = (
        lambda: autoclass("com.flet.serious_python_android.PythonActivity").mActivity,
        lambda: autoclass("android.app.ActivityThread").currentApplication(),
        lambda: autoclass("org.kivy.android.PythonActivity").mActivity,
    )
    for strategy in strategies:
        try:
            context = strategy()
            if context is not None:
                return context
        except Exception:
            continue
    return None


#: A no-op logger used whenever the caller doesn't supply one -- every
#: logging call in this module goes through ``self._log`` rather than a
#: bare ``print``, so a caller that DOES care (main.py, wiring in the
#: app's own LoggerService) gets the full step-by-step trail, and one
#: that doesn't still runs with zero behavior change.
def _noop_log(_message: str) -> None:
    pass


class CompassSensorService:
    """Owns the Android sensor listeners and the latest computed heading.

    Safe to construct on any platform -- construction alone touches
    NOTHING Android/JNI-related, it only sets plain Python attributes.

    Real work happens in two separate, independently-safe stages, per
    the project's staged-rollout requirement:

      * ``probe()`` -- the MINIMAL test: import pyjnius, acquire the
        Android context, acquire SensorManager, detect whether the
        magnetometer/accelerometer/gyroscope exist. Never creates or
        registers a listener. Safe to call repeatedly; cheap.
      * ``start_listening()`` -- only creates the real-time
        ``SensorEventListener`` and registers it. Only ever worth
        calling after ``probe()`` reports both required sensors
        present; still fully self-contained and safe to call even if
        that's not true (it just re-derives that itself).

    Every single step in both is wrapped in its own try/except with a
    distinct log line (see ``_log``), so a failure's exact location is
    always identifiable from the log even without a debugger attached
    to a real device. Both methods ALSO have an outer
    ``except BaseException`` safety net -- broader than the normal
    ``except Exception`` used for the granular per-step handling --
    specifically because this project already hit an undiagnosed
    startup crash from this general area once; the outer net is
    deliberate defense in depth, not normal Python style.
    """

    def __init__(self, log: Optional["Callable[[str], None]"] = None) -> None:
        self._log = log or _noop_log
        self._diagnostics = CompassDiagnostics()
        self._diagnostics_lock = threading.Lock()
        #: Single-slot "latest value" queue -- a heading reading that's
        #: superseded by a newer one before the tick loop drains it is
        #: simply replaced, never queued up as backlog (a compass only
        #: ever needs to show the CURRENT heading).
        self._heading_queue: "queue.Queue[float]" = queue.Queue(maxsize=1)
        self._smoother = _CircularSmoother(_SMOOTHING_ALPHA)
        self._latest_accel: Optional[tuple[float, float, float]] = None
        self._latest_magnetic: Optional[tuple[float, float, float]] = None
        self._sensor_manager = None
        self._magnetic_sensor = None
        self._accel_sensor = None
        self._jnius_module = None
        self._listeners: list = []
        self._probed = False
        self._listening = False
        self._first_callback_logged = False

    @property
    def diagnostics(self) -> CompassDiagnostics:
        """Thread-safe snapshot of the current sensor status."""
        with self._diagnostics_lock:
            return self._diagnostics

    def probe(self) -> CompassDiagnostics:
        """MINIMAL, safe test: import pyjnius, get the Android context,
        get SensorManager, detect magnetometer/accelerometer/gyroscope.

        Deliberately does NOT create or register any
        ``SensorEventListener`` -- that's ``start_listening()``'s job,
        kept separate specifically so a failure can be isolated to
        "detecting sensors" vs. "starting the live listener" (per the
        project's staged-rollout requirement). Safe to call on any
        platform, any number of times; idempotent after the first
        successful/failed attempt.
        """
        if self._probed:
            return self.diagnostics
        self._probed = True

        try:
            self._log("compass: probe() starting")

            self._log("compass: step 1/5 -- importing pyjnius")
            jnius_module = _import_jnius()
            if jnius_module is None:
                self._log("compass: pyjnius not available (expected on desktop/web) -- compass unavailable")
                return self.diagnostics
            self._jnius_module = jnius_module
            self._log("compass: pyjnius imported OK")

            self._log("compass: step 2/5 -- acquiring Android context")
            context = None
            try:
                context = _resolve_android_context(jnius_module)
            except Exception as exc:
                self._log(f"compass: context acquisition raised: {exc!r}")
            if context is None:
                self._log("compass: no Android context available -- compass unavailable")
                return self.diagnostics
            self._log("compass: Android context acquired OK")

            self._log("compass: step 3/5 -- acquiring SensorManager")
            try:
                autoclass = jnius_module.autoclass
                Context = autoclass("android.content.Context")
                sensor_manager = context.getSystemService(Context.SENSOR_SERVICE)
            except Exception as exc:
                self._log(f"compass: SensorManager acquisition raised: {exc!r}")
                return self.diagnostics
            if sensor_manager is None:
                self._log("compass: SensorManager unavailable -- compass unavailable")
                return self.diagnostics
            self._sensor_manager = sensor_manager
            self._log("compass: SensorManager acquired OK")

            self._log("compass: step 4/5 -- detecting magnetometer/accelerometer")
            try:
                Sensor = autoclass("android.hardware.Sensor")
                magnetic_sensor = sensor_manager.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD)
                accel_sensor = sensor_manager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
            except Exception as exc:
                self._log(f"compass: sensor detection (magnetometer/accelerometer) raised: {exc!r}")
                return self.diagnostics
            self._magnetic_sensor = magnetic_sensor
            self._accel_sensor = accel_sensor
            self._log(
                f"compass: magnetometer={'present' if magnetic_sensor is not None else 'absent'}, "
                f"accelerometer={'present' if accel_sensor is not None else 'absent'}"
            )

            self._log("compass: step 5/5 -- detecting gyroscope (diagnostics only)")
            gyro_sensor = None
            try:
                gyro_sensor = sensor_manager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
            except Exception as exc:
                self._log(f"compass: gyroscope detection raised (non-fatal): {exc!r}")
            self._log(f"compass: gyroscope={'present' if gyro_sensor is not None else 'absent'}")

            with self._diagnostics_lock:
                self._diagnostics = CompassDiagnostics(
                    magnetometer_available=magnetic_sensor is not None,
                    accelerometer_available=accel_sensor is not None,
                    gyroscope_available=gyro_sensor is not None,
                )
            self._log(f"compass: probe() finished -- {self._diagnostics}")

        except BaseException as exc:  # noqa: BLE001 -- deliberate outer safety net, see class docstring
            self._log(f"compass: probe() failed with an unexpected error, disabling compass: {exc!r}")
            with self._diagnostics_lock:
                self._diagnostics = CompassDiagnostics()

        return self.diagnostics

    def start_listening(self) -> CompassDiagnostics:
        """Create and register the real-time ``SensorEventListener``.

        Only meaningful after a successful ``probe()`` found both
        required sensors -- calling it otherwise is safe (it just
        re-derives that nothing is available) but does nothing useful.
        Kept entirely separate from ``probe()`` so a failure HERE
        (listener/interface creation is the part most likely to be
        JNI-version-sensitive) never prevents the app from at least
        knowing which sensors exist.
        """
        if self._listening:
            return self.diagnostics
        self._listening = True

        try:
            if self._jnius_module is None or self._sensor_manager is None:
                self._log("compass: start_listening() skipped -- probe() didn't succeed")
                return self.diagnostics
            if self._magnetic_sensor is None or self._accel_sensor is None:
                self._log("compass: start_listening() skipped -- required sensor(s) missing")
                return self.diagnostics

            self._log("compass: step 6/8 -- creating SensorEventListener")
            try:
                callback = _SensorCallback(self._jnius_module, self._handle_sensor_reading, self._log)
            except Exception as exc:
                self._log(f"compass: SensorEventListener creation failed: {exc!r}")
                return self.diagnostics
            self._log("compass: SensorEventListener created OK")

            self._log("compass: step 7/8 -- registering sensor listeners")
            try:
                self._sensor_manager.registerListener(callback, self._magnetic_sensor, _SENSOR_DELAY_NORMAL)
                self._sensor_manager.registerListener(callback, self._accel_sensor, _SENSOR_DELAY_NORMAL)
            except Exception as exc:
                self._log(f"compass: sensor registration failed: {exc!r}")
                return self.diagnostics
            self._listeners.append(callback)
            self._log("compass: sensor listeners registered OK -- step 8/8 (first callback) pending real device motion")

        except BaseException as exc:  # noqa: BLE001 -- deliberate outer safety net, see class docstring
            self._log(f"compass: start_listening() failed with an unexpected error, disabling compass: {exc!r}")
            with self._diagnostics_lock:
                self._diagnostics = CompassDiagnostics()

        return self.diagnostics

    def stop(self) -> None:
        """Unregister sensor listeners, if any were registered.

        Good practice (sensors drain battery if left registered), and
        safe to call even if sensors never actually got started.
        """
        if self._sensor_manager is None:
            return
        for listener in self._listeners:
            try:
                self._sensor_manager.unregisterListener(listener)
            except Exception:
                pass
        self._listeners.clear()
        self._sensor_manager = None

    def drain_latest_heading(self) -> Optional[float]:
        """Return the most recent computed heading, if a new one has
        arrived since the last call -- else ``None`` (caller should just
        keep showing whatever it last displayed).
        """
        try:
            return self._heading_queue.get_nowait()
        except queue.Empty:
            return None

    # --- Internal: only ever reached after a successful probe() ---------

    def _handle_sensor_reading(self, sensor_type: int, values: tuple[float, ...]) -> None:
        """Called (already on an attached thread -- see ``_SensorCallback``)
        whenever either sensor reports a new value. Recomputes the
        azimuth from the latest of BOTH vectors every time either one
        updates, since a real heading needs both together.
        """
        if not self._first_callback_logged:
            self._first_callback_logged = True
            self._log("compass: step 8/8 -- first sensor callback received OK, compass is live")

        if sensor_type == _TYPE_MAGNETIC_FIELD:
            self._latest_magnetic = (values[0], values[1], values[2])
        elif sensor_type == _TYPE_ACCELEROMETER:
            self._latest_accel = (values[0], values[1], values[2])
        else:
            return

        if self._latest_accel is None or self._latest_magnetic is None:
            return

        raw_heading = _tilt_compensated_azimuth_degrees(self._latest_accel, self._latest_magnetic)
        if raw_heading is None:
            return

        smoothed = self._smoother.update(raw_heading)

        with self._diagnostics_lock:
            self._diagnostics = CompassDiagnostics(
                magnetometer_available=self._diagnostics.magnetometer_available,
                accelerometer_available=self._diagnostics.accelerometer_available,
                gyroscope_available=self._diagnostics.gyroscope_available,
                heading_degrees=smoothed,
            )

        # Single-slot queue: drop any not-yet-consumed older value before
        # pushing the new one, so the tick loop only ever sees the latest.
        try:
            self._heading_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._heading_queue.put_nowait(smoothed)
        except queue.Full:
            pass


class _SensorCallback:
    """Implements Android's ``SensorEventListener`` interface in Python
    via pyjnius's ``PythonJavaClass`` -- no separate .java/.kt file
    needed for this.

    Built lazily (only when ``CompassSensorService.start_listening()``
    actually runs on Android with a confirmed working pyjnius import)
    so importing THIS module never requires ``jnius`` to be
    installed/importable, and constructing a ``CompassSensorService``
    never touches it either -- only calling ``start_listening()`` does.

    Thread safety (per the project's explicit requirement): this class
    NEVER touches any Flet UI control, directly or indirectly --
    ``on_reading`` (``CompassSensorService._handle_sensor_reading``)
    only writes to plain Python attributes (protected by a lock) and a
    thread-safe ``queue.Queue``. The only place that queue is ever read
    is main.py's existing 1-second tick loop, which runs on the app's
    own asyncio loop -- never this callback thread.
    """

    def __new__(cls, jnius_module, on_reading, log: Callable[[str], None]):
        PythonJavaClass = jnius_module.PythonJavaClass
        java_method = jnius_module.java_method
        #: Tracks whether THIS specific OS thread has already been
        #: attached -- Android delivers sensor events on its own
        #: dedicated thread, consistently, so in practice this reduces
        #: attach_thread() from "once per event" (every ~200ms) to
        #: "once, ever" per callback's lifetime, cutting JNI call volume
        #: substantially. Calling attach_thread() repeatedly on an
        #: already-attached thread is a documented no-op per the JNI
        #: spec, but avoiding the redundant call entirely removes any
        #: doubt about whether pyjnius's specific wrapper handles that
        #: no-op path cleanly under Serious Python's embedding.
        thread_local = threading.local()

        class _Impl(PythonJavaClass):
            __javainterfaces__ = ["android/hardware/SensorEventListener"]
            __javacontext__ = "app"

            def __init__(self, callback):
                super().__init__()
                self._callback = callback

            @java_method("(Landroid/hardware/SensorEvent;)V")
            def onSensorChanged(self, event):
                # The single most important safety boundary in this
                # entire module: NOTHING below this point may ever
                # raise past this callback, because an uncaught
                # exception (or worse, something below Python's normal
                # Exception hierarchy) crossing back into Android's own
                # event-dispatch code is exactly what can take down the
                # whole process rather than just this callback. Hence
                # `except BaseException`, not the narrower `Exception`
                # used everywhere else in this module.
                try:
                    if not getattr(thread_local, "attached", False):
                        jnius_module.attach_thread()
                        thread_local.attached = True
                    sensor_type = event.sensor.getType()
                    values = tuple(float(v) for v in event.values)
                    self._callback(sensor_type, values)
                except BaseException as exc:  # noqa: BLE001 -- deliberate outer safety net, see class docstring
                    try:
                        log(f"compass: onSensorChanged failed (reading dropped, compass stays running): {exc!r}")
                    except Exception:
                        pass

            @java_method("(Landroid/hardware/Sensor;I)V")
            def onAccuracyChanged(self, sensor, accuracy):
                pass

        return _Impl(on_reading)
