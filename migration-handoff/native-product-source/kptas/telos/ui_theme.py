"""Native QSS adapter for the shared TAS semantic color roles.

The packaged JSON also produces the browser's CSS variables. QSS has no CSS
custom properties, so values resolve once at import, never per paint.
User QSS is still appended last by Overlay.apply_ui_preferences().
"""
from importlib.resources import files
import json
from string import Template
from types import MappingProxyType


def _rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _colors():
    data = json.loads(files(__package__).joinpath('ui_tokens.json').read_text(encoding='utf-8'))
    if data['schema'] != 'stasis-ui-colors/v1':
        raise ValueError('Unsupported Stasis UI color schema')
    values = dict(data['colors'])
    for name, tint in data['tints'].items():
        red, green, blue = _rgb(data['colors'][tint['color']])
        alpha = tint['alpha']
        if not 0 <= alpha <= 1:
            raise ValueError(f'Invalid Stasis UI alpha: {name}')
        # Emit QSS's 8-bit alpha syntax; keep the JSON's fractional CSS alpha.
        values[name] = f'rgba({red}, {green}, {blue}, {int(alpha * 255 + 0.5)})'
    line = data['tints']['line']
    foreground = _rgb(data['colors'][line['color']])
    background = _rgb(data['colors']['panel'])
    # Opaque fallback over the default panel, derived from the same roles.
    values['line_opaque'] = '#' + ''.join(
        f'{int(front * line["alpha"] + back * (1 - line["alpha"]) + 0.5):02x}'
        for front, back in zip(foreground, background)
    )
    for name, number in data.get('metrics', {}).items():
        if type(number) is not int or not 1 <= number <= 128:
            raise ValueError(f'Invalid Stasis UI metric: {name}')
        values[name] = f'{number}px'
    for name, families in data.get('fonts', {}).items():
        if not isinstance(families, list) or not families or any(
            not isinstance(family, str) or not family or not all(c.isalnum() or c in ' -' for c in family)
            for family in families
        ):
            raise ValueError(f'Invalid Stasis UI font: {name}')
        values['font_' + name] = ', '.join('"' + family + '"' for family in families)
    return MappingProxyType(values)


COLORS = _colors()


def qss(template: str) -> str:
    """Resolve an application-owned template; never rewrite a user's QSS."""
    return Template(template).substitute(COLORS)
