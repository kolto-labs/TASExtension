"""Portable presentation profile; deliberately contains no execution controls."""
from copy import deepcopy
from dataclasses import replace
import json
import math
import re

FORMAT = 'telos-shared-presentation/v4'
PREVIOUS_FORMAT = 'telos-shared-presentation/v3'
GAME_ONLY_FORMAT = 'telos-shared-presentation/v2'
LEGACY_FORMAT = 'telos-shared-presentation/v1'
LIMIT = 64 * 1024


def validate_profile(value):
    def exact(item, keys, label):
        if not isinstance(item, dict) or set(item) - set(keys):
            raise ValueError(f'Unknown or invalid {label} fields')
    exact(value, ('format', 'presentation', 'panels', 'hostProfiles'), 'profile')
    if value.get('format') not in (FORMAT, PREVIOUS_FORMAT, GAME_ONLY_FORMAT, LEGACY_FORMAT) or value.get('presentation') not in ('overlay', 'surround'):
        raise ValueError('Unsupported shared presentation profile')
    legacy = value['format'] == LEGACY_FORMAT
    panels = value.get('panels')
    density = value['format'] == FORMAT
    names = ('game', 'timeline') if value['format'] in (FORMAT, PREVIOUS_FORMAT) else ('game',)
    exact(panels, names, 'panels')
    for name in names:
        panel = panels.get(name)
        if name == 'timeline':
            exact(panel, ('opacity', 'density') if density else ('opacity',), 'timeline panel')
            opacity = panel.get('opacity')
            if type(opacity) not in (int, float) or not math.isfinite(opacity) or not .55 <= opacity <= 1:
                raise ValueError('timeline opacity must be 0.55–1')
            if density and panel.get('density') not in ('editor', 'timeline', 'minimal'):
                raise ValueError('Unsupported timeline density')
            continue
        exact(panel, ('visible', 'opacity') if legacy else ('visible', 'opacity', 'collapsed'), name + ' panel')
        opacity = panel.get('opacity')
        if type(panel.get('visible')) is not bool or type(opacity) not in (int, float) or not math.isfinite(opacity) or not .55 <= opacity <= 1:
            raise ValueError(f'{name} visibility must be boolean and opacity must be 0.55–1')
        if not legacy and type(panel.get('collapsed')) is not bool:
            raise ValueError(f'{name} collapsed state must be boolean')
    hosts = value.get('hostProfiles')
    if not isinstance(hosts, dict) or any(not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', key) for key in hosts):
        raise ValueError('Invalid host profile identifiers')
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > LIMIT:
        raise ValueError('Shared profiles must be 64 KiB or smaller')
    return deepcopy(value)


def export_profile(overlay, host_profiles):
    states = overlay.panel_manager.get_states()
    panels = {'game': {key: states['preview'][key] for key in ('visible', 'opacity', 'collapsed')}}
    panels['timeline'] = {'opacity': states['transport']['opacity'], 'density': overlay.ui_preferences.density}
    hosts = deepcopy(host_profiles)
    hosts['native'] = {'layout': overlay.ui_preferences.layout, 'panel_states': states}
    return validate_profile({'format': FORMAT,
        'presentation': 'overlay' if overlay.ui_preferences.layout == 'overlay' else 'surround',
        'panels': panels,
        'hostProfiles': hosts})


def apply_profile(overlay, document):
    """Apply presentation only; retain geometry, shortcuts, input owner and pixels.

    This deliberately does not use apply_ui_preferences: that path reloads
    shortcuts and invalidates preview pixels. Opaque host payloads are retained
    by the caller for export but are never applied here.
    """
    document = validate_profile(document)
    manager, root, C = overlay.panel_manager, overlay.root, overlay.C
    if 'preview' not in manager.panels:
        raise ValueError('This native host does not have a game preview panel')
    original = overlay.ui_preferences
    states = manager.get_states()
    geometry = root.geometry()
    was_visible = root.isVisible()
    target_states = deepcopy(states)
    target_states['preview'].update(document['panels']['game'])
    if 'timeline' in document['panels']:
        target_states['transport']['opacity'] = document['panels']['timeline']['opacity']
    layout = 'overlay' if document['presentation'] == 'overlay' else 'workspace'
    density = document['panels'].get('timeline', {}).get('density', original.density)
    if document['format'] == FORMAT:
        # Density is owned by the native rail/HUD/editor controller. It is not
        # inferred from imported dock collapse and does not grant any command.
        target_states['transport'].update(visible=density == 'editor', collapsed=False)
    candidate = replace(original, layout=layout, panel_states=target_states,
                        density=density,
                        panels=replace(original.panels, preview=document['panels']['game']['visible'], editor=density == 'editor'))
    from .overlay import STYLE

    def apply(preferences, panel_states):
        overlay.ui_preferences = preferences
        workspace = preferences.layout == 'workspace'
        opaque = workspace or preferences.game_surface == 'owner_frame'
        flags = C.Qt.WindowType.Window if workspace else (C.Qt.WindowType.FramelessWindowHint | C.Qt.WindowType.WindowStaysOnTopHint | C.Qt.WindowType.Tool)
        root.setWindowFlags(flags)
        root.setAttribute(C.Qt.WidgetAttribute.WA_TranslucentBackground, not opaque)
        root.setAttribute(C.Qt.WidgetAttribute.WA_NoSystemBackground, not opaque)
        root.setAttribute(C.Qt.WidgetAttribute.WA_StyledBackground, opaque)
        root.setAttribute(C.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        root.setProperty('layoutPreset', preferences.layout)
        root.setStyleSheet(STYLE + ('\nQWidget#telosRoot { background:#202329; }' if opaque else '') + '\n' + preferences.stylesheet)
        overlay.origin.setVisible(workspace)
        overlay.input_title.setVisible(workspace)
        overlay.saved.setVisible(workspace)
        overlay.hint.setVisible(workspace)
        manager.set_menu_anchor((8, 8) if workspace else (8, 136))
        root.setGeometry(geometry)
        overlay.set_density(preferences.density)
        overlay._layout_surface(root.width(), root.height())
        manager.set_states(panel_states)
        overlay.ui_preferences = preferences
        overlay._layout_seek_rail()
        root.setGeometry(geometry)
        if was_visible:
            root.show()
            overlay._restore_platform_mask()

    root.setUpdatesEnabled(False)
    previous_applying = overlay._applying_panels
    overlay._applying_panels = True
    try:
        try:
            apply(candidate, target_states)
        except Exception:
            apply(original, states)
            raise
    finally:
        overlay._applying_panels = previous_applying
        root.setUpdatesEnabled(True)
