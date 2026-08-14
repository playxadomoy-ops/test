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
from typing import Optional

#: Android sensor type constants (android.hardware.Sensor), inlined so
#: this module doesn't need a live JNI connection just to know their
#: values -- these are long-stable, publicly documented Android SDK
#: constants, not something that varies by device.
_TYPE_ACCELEROMETER = 1
_TYPE_MAGNETIC_FIELD = 2
_TYPE_GYROSCOPE = 4

#: SensorManager.SENSOR_DELAY_UI -- a sampling rate appropriate for
#: driving a visible UI element (not the fastest possible rate, which
#: would waste battery for no visible benefit on a compass display).
_SENSOR_DELAY_UI = 1

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


class CompassSensorService:
    """Owns the Android sensor listeners and the latest computed heading.

    Safe to construct and call ``start()`` on any platform -- it only
    ever does real work when actually running on Android with both
    required sensors present; every other case leaves it in the same
    explicit "unavailable" diagnostic state.
    """

    def __init__(self) -> None:
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
        self._listeners: list = []
        self._started = False

    @property
    def diagnostics(self) -> CompassDiagnostics:
        """Thread-safe snapshot of the current sensor status."""
        with self._diagnostics_lock:
            return self._diagnostics

    def start(self) -> CompassDiagnostics:
        """Attempt to initialize real Android sensors. Always safe to
        call, on any platform -- returns the resulting diagnostics
        either way (also available afterwards via ``.diagnostics``).

        Idempotent: calling this more than once after a successful start
        just returns the current diagnostics without re-registering.
        """
        if self._started:
            return self.diagnostics

        jnius_module = _import_jnius()
        if jnius_module is None:
            # Not Android (or pyjnius genuinely isn't present) -- leave
            # the default "everything unavailable" diagnostics as-is.
            return self.diagnostics

        try:
            self._start_android(jnius_module)
        except Exception:
            # Any failure anywhere in real device/JNI setup -> the
            # honest "unavailable" state, never a half-working guess.
            with self._diagnostics_lock:
                self._diagnostics = CompassDiagnostics()
        finally:
            self._started = True

        return self.diagnostics

    def stop(self) -> None:
        """Unregister sensor listeners, if any were registered.

        Good practice (sensors drain battery if left registered), and
        safe to call even if ``start()`` never actually got Android
        sensors running.
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

    # --- Internal: only ever reached when jnius actually imported -------

    def _start_android(self, jnius_module) -> None:
        autoclass = jnius_module.autoclass

        context = _resolve_android_context(jnius_module)
        if context is None:
            return  # leave diagnostics at the default "unavailable" state

        Context = autoclass("android.content.Context")
        sensor_manager = context.getSystemService(Context.SENSOR_SERVICE)
        if sensor_manager is None:
            return
        Sensor = autoclass("android.hardware.Sensor")

        magnetic_sensor = sensor_manager.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD)
        accel_sensor = sensor_manager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        gyro_sensor = sensor_manager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)

        diagnostics = CompassDiagnostics(
            magnetometer_available=magnetic_sensor is not None,
            accelerometer_available=accel_sensor is not None,
            gyroscope_available=gyro_sensor is not None,
        )
        with self._diagnostics_lock:
            self._diagnostics = diagnostics

        if magnetic_sensor is None or accel_sensor is None:
            return  # a real heading needs both; diagnostics already reflect this

        self._sensor_manager = sensor_manager
        callback = _SensorCallback(self._handle_sensor_reading)
        sensor_manager.registerListener(callback, magnetic_sensor, _SENSOR_DELAY_UI)
        sensor_manager.registerListener(callback, accel_sensor, _SENSOR_DELAY_UI)
        self._listeners.append(callback)

    def _handle_sensor_reading(self, sensor_type: int, values: tuple[float, ...]) -> None:
        """Called (already on an attached thread -- see ``_SensorCallback``)
        whenever either sensor reports a new value. Recomputes the
        azimuth from the latest of BOTH vectors every time either one
        updates, since a real heading needs both together.
        """
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

    Built lazily (only when actually starting on Android) so importing
    this module never requires ``jnius`` to be installed/importable.
    """

    def __new__(cls, on_reading):
        jnius_module = _import_jnius()
        if jnius_module is None:
            raise RuntimeError("_SensorCallback requires Android/pyjnius")

        PythonJavaClass = jnius_module.PythonJavaClass
        java_method = jnius_module.java_method

        class _Impl(PythonJavaClass):
            __javainterfaces__ = ["android/hardware/SensorEventListener"]
            __javacontext__ = "app"

            def __init__(self, callback):
                super().__init__()
                self._callback = callback

            @java_method("(Landroid/hardware/SensorEvent;)V")
            def onSensorChanged(self, event):
                # Required before touching any Python/JNI state from a
                # callback invoked on an Android-owned thread -- see
                # module docstring.
                try:
                    jnius_module.attach_thread()
                except Exception:
                    pass
                try:
                    sensor_type = event.sensor.getType()
                    values = tuple(float(v) for v in event.values)
                    self._callback(sensor_type, values)
                except Exception:
                    pass  # a single bad reading must never crash the app

            @java_method("(Landroid/hardware/Sensor;I)V")
            def onAccuracyChanged(self, sensor, accuracy):
                pass

        return _Impl(on_reading)
