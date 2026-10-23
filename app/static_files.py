"""Cache versioned public assets and compress text assets only."""
from urllib.parse import parse_qs, urlsplit

from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

from .templating import asset


class VersionedStaticFiles(StaticFiles):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._compressed = GZipMiddleware(super().__call__, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope.get('path', '').endswith(('.css', '.js', '.svg')):
            await self._compressed(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            query = parse_qs(scope.get('query_string', b'').decode('latin-1'))
            current = parse_qs(urlsplit(asset(path)).query)
            response.headers['Cache-Control'] = ('public, max-age=31536000, immutable'
                                                 if query.get('v') == current.get('v') else 'no-cache')
        return response
