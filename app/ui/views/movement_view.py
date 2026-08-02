"""'Рух загроз' (Threat Movement) tab content.

Shows the movement map at the top, a scrollable list of all currently
active movements below it, and (per the Threat Movement redesign) an
oblast picker further below that: selecting one or more oblasts there
crops/zooms the map above to just that selection instead of the whole
country. Every list entry comes straight from
:mod:`app.services.movement_parser` -- no prediction, no invented
routes; entries without a resolvable place simply aren't drawn on the
map but still appear in this list, since their type/time/channel/text
are still real, explicit information from the message.
"""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft

from app.models.alert_models import Region
from app.models.movement_models import ThreatMovement
from app.ui.components.movement_map import MovementMap, OnMovementTapped, OnRegionTapped
from app.ui.components.ukraine_map import OnDistrictTapped
from app.ui.theme import colors as theme

_MAX_VISIBLE_ENTRIES = 100

OnSelectedRegionsChanged = Callable[[set[Region]], None]


class MovementView(ft.Column):
    """The 'Рух загроз' tab: map, active-messages list, then oblast picker."""

    def __init__(
        self,
        on_movement_tap: Optional[OnMovementTapped] = None,
        on_region_tap: Optional[OnRegionTapped] = None,
        on_district_tap: Optional[OnDistrictTapped] = None,
        on_selected_regions_changed: Optional[OnSelectedRegionsChanged] = None,
    ) -> None:
        """Build the tab shell; call :meth:`set_movements`/:meth:`set_selected_regions` to populate it."""
        self.movement_map = MovementMap(
            on_movement_tap=on_movement_tap, on_region_tap=on_region_tap, on_district_tap=on_district_tap
        )

        self._list_column = ft.Column(spacing=8)
        self._empty_text = ft.Row(
            spacing=8,
            controls=[
                ft.Icon(ft.Icons.RADAR, size=16, color=theme.TEXT_MUTED),
                ft.Text(
                    "Активних повідомлень про рух загроз ще немає.",
                    size=12,
                    color=theme.TEXT_MUTED,
                ),
            ],
        )
        self._on_movement_tap = on_movement_tap
        self._on_selected_regions_changed = on_selected_regions_changed

        self._all_movements: list[ThreatMovement] = []
        self._selected_regions: set[Region] = set()

        # NOTE: ``ft.FilterChip`` does not exist in flet==0.28.3 (verified
        # against the installed package) -- only ``ft.Chip``, toggled
        # manually via ``on_click``. Same pattern this project already
        # used for the (now removed) Налаштування oblast picker.
        self._region_chips: dict[Region, ft.Chip] = {
            region: ft.Chip(
                label=ft.Text(region.value, size=11),
                selected=False,
                selected_color=theme.ACCENT_PURPLE,
                show_checkmark=True,
                on_click=self._make_chip_toggle_handler(region),
            )
            for region in Region
        }

        super().__init__(
            spacing=12,
            expand=True,
            scroll=ft.ScrollMode.ADAPTIVE,
            controls=[
                self.movement_map,
                ft.Container(
                    padding=16,
                    border_radius=theme.RADIUS_LG,
                    bgcolor=theme.SURFACE_ELEVATED,
                    border=ft.border.all(1, theme.BORDER),
                    shadow=theme.elevation_shadow(),
                    content=ft.Column(
                        spacing=8,
                        controls=[
                            ft.Text(
                                "Активні повідомлення",
                                size=14,
                                weight=ft.FontWeight.W_600,
                                color=theme.TEXT_SECONDARY,
                            ),
                            self._empty_text,
                            self._list_column,
                        ],
                    ),
                ),
                ft.Container(
                    padding=16,
                    border_radius=theme.RADIUS_LG,
                    bgcolor=theme.SURFACE_ELEVATED,
                    border=ft.border.all(1, theme.BORDER),
                    shadow=theme.elevation_shadow(),
                    content=ft.Column(
                        spacing=8,
                        controls=[
                            ft.Text(
                                "Обрані області",
                                size=14,
                                weight=ft.FontWeight.W_600,
                                color=theme.TEXT_SECONDARY,
                            ),
                            ft.Text(
                                "Оберіть одну чи кілька областей, щоб карта вище показувала "
                                "лише їх, наближено. Без вибору -- карта показує всю Україну.",
                                size=11,
                                color=theme.TEXT_MUTED,
                            ),
                            ft.Row(
                                wrap=True,
                                spacing=6,
                                run_spacing=6,
                                controls=list(self._region_chips.values()),
                            ),
                        ],
                    ),
                ),
            ],
        )

    def set_movements(self, movements: list[ThreatMovement]) -> None:
        """Replace the currently displayed set of active movements."""
        self._all_movements = movements
        self._render()

    def set_selected_regions(self, regions: set[Region]) -> None:
        """Apply a selection loaded from storage (or cleared on reset) to the chips + map."""
        if regions == self._selected_regions:
            return
        self._selected_regions = set(regions)
        for region, chip in self._region_chips.items():
            chip.selected = region in self._selected_regions
            if self.page is not None:
                chip.update()
        self.movement_map.set_selected_regions(self._selected_regions)

    def _make_chip_toggle_handler(self, region: Region) -> Callable[[ft.ControlEvent], None]:
        """Build a click handler that flips one region chip and re-applies the selection."""

        def handler(_: ft.ControlEvent) -> None:
            chip = self._region_chips[region]
            chip.selected = not chip.selected
            if self.page is not None:
                chip.update()

            if chip.selected:
                self._selected_regions.add(region)
            else:
                self._selected_regions.discard(region)

            self.movement_map.set_selected_regions(self._selected_regions)
            if self._on_selected_regions_changed is not None:
                self._on_selected_regions_changed(set(self._selected_regions))

        return handler

    def _render(self) -> None:
        """Push the (always unfiltered) movement list to the map + side list."""
        self.movement_map.update_movements(self._all_movements)

        ordered = sorted(self._all_movements, key=lambda m: m.received_at, reverse=True)[
            :_MAX_VISIBLE_ENTRIES
        ]
        self._list_column.controls = [self._build_row(m) for m in ordered]
        self._empty_text.visible = len(ordered) == 0

        if self.page is not None:
            self._list_column.update()
            self._empty_text.update()

    def _build_row(self, movement: ThreatMovement) -> ft.Control:
        """One row in the side list: icon, time, channel, direction, tap to open."""
        return ft.Container(
            padding=10,
            border_radius=10,
            bgcolor=theme.SURFACE,
            ink=True,
            on_click=lambda e, m=movement: self._handle_tap(m),
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(movement.threat_type.icon, size=20),
                    ft.Column(
                        spacing=2,
                        expand=True,
                        controls=[
                            ft.Row(
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                controls=[
                                    ft.Text(
                                        movement.threat_type.label_uk,
                                        size=12,
                                        weight=ft.FontWeight.W_600,
                                        color=theme.TEXT_PRIMARY,
                                    ),
                                    ft.Text(
                                        f"{movement.received_at:%H:%M:%S}",
                                        size=10,
                                        color=theme.TEXT_MUTED,
                                    ),
                                ],
                            ),
                            ft.Text(
                                movement.short_description,
                                size=11,
                                color=theme.ACCENT_BLUE,
                            ),
                            ft.Text(
                                movement.channel_username,
                                size=10,
                                color=theme.TEXT_MUTED,
                            ),
                        ],
                    ),
                ],
            ),
        )

    def _handle_tap(self, movement: ThreatMovement) -> None:
        if self._on_movement_tap is not None:
            self._on_movement_tap(movement)
