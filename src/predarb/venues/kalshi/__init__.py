"""Kalshi adapter.

Modules (planned unless noted):

``auth``
    RSA-PSS/SHA-256 request signing. Signs ``timestamp_ms + METHOD + path``
    with the query string stripped.
``client``
    Async REST client over httpx, with rate-limit budgeting.
``websocket``
    ``orderbook_delta`` subscription, sequence tracking, reconnect/resnapshot.
``models``
    Pydantic v2 models mirroring the wire schema exactly, including the
    ``_fp``/``_dollars`` string fields. These parse to ``str`` and convert to
    :mod:`predarb.domain.money` types explicitly -- never to ``float``.
``normalize``
    Wire models to domain types.
``fees``
    Fee computation driven by versioned fee-schedule metadata.

See ``docs/kalshi_adapter.md`` and ``docs/api_assumptions.md``.
"""
