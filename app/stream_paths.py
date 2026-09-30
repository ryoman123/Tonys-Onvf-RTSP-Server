"""Keep recorder, browser and local detector stream consumers consistent."""
from urllib.parse import quote, urlsplit, urlunsplit


def stream_encoding(camera, kind):
    if getattr(camera, 'transcode_' + kind, False):
        return 'H264'
    return getattr(camera, kind + '_encoding', 'H264')


def stream_path(camera, kind='sub', *, browser=False):
    if kind == 'sub' and getattr(camera, 'disable_substream', False):
        kind = 'main'
    path = f'{camera.path_name}_{kind}'
    if browser and stream_encoding(camera, kind) == 'H265':
        path += '_browser'
    return path


def internal_rtsp_url(camera, kind='sub', *, port=None, username=None, password=None):
    manager = getattr(camera, 'manager', None)
    if port is None:
        port = getattr(manager, 'rtsp_port', camera.rtsp_port)
    if username is None and getattr(manager, 'rtsp_auth_enabled', False):
        username = getattr(manager, 'global_username', '') or 'admin'
        password = getattr(manager, 'global_password', '') or 'admin'
    auth = ''
    if username:
        auth = quote(str(username), safe='') + ':' + quote(str(password or ''), safe='') + '@'
    return f'rtsp://{auth}127.0.0.1:{port}/{stream_path(camera, kind)}'


def redact_rtsp_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))
