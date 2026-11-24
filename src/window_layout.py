"""Window coordinates are physical pixels; remembered geometry is in DIPs."""


def clamp_rect(rect, work):
    x, y, width, height = rect
    left, top, right, bottom = work
    width, height = min(width, right - left), min(height, bottom - top)
    return (min(max(x, left), right - width), min(max(y, top), bottom - height), width, height)


def remember_rect(rect, work, scale):
    x, y, width, height = rect
    return dict(x=(x - work[0]) / scale, y=(y - work[1]) / scale,
                width=width / scale, height=height / scale)


def restore_rect(saved, work, scale):
    rect = (work[0] + round(saved['x'] * scale), work[1] + round(saved['y'] * scale),
            round(saved['width'] * scale), round(saved['height'] * scale))
    return clamp_rect(rect, work)


def choose_monitor(screens, pinned, point):
    selected = next((screen for screen in screens if screen['id'] == pinned), None)
    if selected:
        return selected
    x, y = point
    selected = next((screen for screen in screens
                     if screen['bounds'][0] <= x < screen['bounds'][2] and screen['bounds'][1] <= y < screen['bounds'][3]), None)
    return selected or next((screen for screen in screens if screen.get('primary')), screens[0] if screens else None)
