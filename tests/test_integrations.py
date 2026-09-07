import httpx

from crowbarr.config import Connection
from crowbarr.integrations import check_connection, refresh_plex


def test_arr_connection_uses_header_not_query_secret(monkeypatch):
    real_client = httpx.Client
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"version": "test"})

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    assert check_connection("sonarr", Connection(url="http://sonarr", api_key="secret"))["ok"]
    assert seen[0].url.path == "/api/v3/system/status"
    assert seen[0].headers["X-Api-Key"] == "secret"
    assert "secret" not in str(seen[0].url)


def test_plex_refreshes_movie_and_show_sections_only(monkeypatch):
    real_client = httpx.Client
    seen = []

    def handler(request):
        seen.append(request.url.path)
        assert request.headers["X-Plex-Token"] == "secret"
        return httpx.Response(
            200,
            text='<MediaContainer><Directory key="1" type="movie"/>'
            '<Directory key="2" type="show"/><Directory key="3" type="artist"/></MediaContainer>',
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    refresh_plex(Connection(url="http://plex", api_key="secret"))
    assert seen == ["/library/sections", "/library/sections/1/refresh", "/library/sections/2/refresh"]
