'''The driver seam: everything ``purecdp.testing`` needs from a browser page,
as a protocol-neutral surface (M20 phase 0).

Page / Frame / Element / Live / intercept / recorder / _agent talk to a
:class:`Driver`, never to a wire protocol module. One implementation exists
today — :mod:`._cdp_driver` over a CDP :class:`~purecdp.connection.Session` —
and the shape is what a WebDriver BiDi backend (Firefox) plugs into later.

A Driver instance IS a scope: one per page or per cross-origin frame (CDP: a
session; BiDi: a browsing context). Browser-level lifecycle (new tabs,
contexts) stays in :mod:`purecdp.browser`.

Values crossing the seam are plain Python: JSON data, :class:`Handle`s for
remote objects, ``bytes`` for binary payloads, and the small dataclasses
below for events. Features a backend cannot provide raise :class:`NotSupported`
at the call, naming the feature and the backend, so a drive that never uses
them never notices.
'''

from __future__ import annotations

import typing
from dataclasses import dataclass, field

from .._shared import Handle  # noqa: F401 (re-export)
from ..errors import NotSupported, PureCDPError  # noqa: F401 (re-export)


class JSError(PureCDPError):
    '''A JavaScript exception, surfaced from evaluate() or captured uncaught.
    ``details`` is the backend's raw exception record (CDP:
    ``protocol.runtime.ExceptionDetails``).'''

    def __init__(self, message: str, details: typing.Any = None):
        super().__init__(message)
        self.details = details

    @classmethod
    def from_details(cls, details: typing.Any) -> JSError:
        '''From a CDP ``ExceptionDetails``.'''
        exception = details.exception
        if exception is not None and exception.description:
            message = exception.description
        elif exception is not None and exception.value is not None:
            message = str(exception.value)
        else:
            message = details.text
            if details.url:
                message += f' ({details.url}:{details.line_number})'
        return cls(message, details)


# ``Handle`` and ``NotSupported`` are core types (purecdp._shared /
# purecdp.errors) so the BiDi package can use them without importing the
# testing layer; they are re-exported here, where the seam is documented.


# -- normalized events -------------------------------------------------------
# One vocabulary for every backend. Each dataclass is one ``kind``; drivers
# emit only the kinds a stream subscribed to (``Driver.listen(*kinds)``).

@dataclass(slots=True)
class Console:
    kind: str                       # log, warning, error, ...
    text: str                       # arguments stringified and space-joined


@dataclass(slots=True)
class Exception_:
    '''An uncaught page exception; ``error`` is a ready-made ``JSError``.'''
    error: Exception


@dataclass(slots=True)
class Load:
    '''The main document finished loading (Page.loadEventFired).'''


@dataclass(slots=True)
class FrameStopped:
    '''A frame stopped loading (Page.frameStoppedLoading).'''
    frame_id: str | None = None


@dataclass(slots=True)
class Dialog:
    message: str
    type: str = 'alert'
    default_prompt: str | None = None


@dataclass(slots=True)
class Request:
    '''A request is going out. ``headers`` are what the base event carried;
    a backend that learns the wire headers later (CDP's *ExtraInfo*) sends a
    :class:`RequestExtra` for the same ``request_id``.'''
    request_id: str
    url: str
    method: str
    headers: dict
    post_data: str | None
    resource_type: str | None
    frame_id: str | None
    timestamp: float               # backend monotonic seconds
    wall_time: float               # unix seconds
    #: Size of a body the backend saw but did not put on the event (BiDi);
    #: ``Driver.request_body`` fetches it. None when unknown / not applicable.
    body_size: int | None = None


@dataclass(slots=True)
class RequestExtra:
    request_id: str
    headers: dict


@dataclass(slots=True)
class Response:
    request_id: str
    status: int
    mime: str
    headers: dict


@dataclass(slots=True)
class ResponseExtra:
    request_id: str
    headers: dict


@dataclass(slots=True)
class Finished:
    request_id: str
    timestamp: float
    encoded_length: int


@dataclass(slots=True)
class Failed:
    request_id: str
    timestamp: float
    error_text: str
    canceled: bool = False


@dataclass(slots=True)
class Paused:
    '''A request held at the interception point. ``post_data`` is the body
    as bytes when the request has one (text or binary, assembled from
    whatever the backend sent).'''
    request_id: str
    url: str
    method: str
    headers: dict
    post_data: bytes | None
    resource_type: str | None
    frame_id: str | None = None
    body_size: int | None = None   # see Request.body_size


@dataclass(slots=True)
class DownloadBegin:
    guid: str
    url: str
    suggested_filename: str


@dataclass(slots=True)
class DownloadProgress:
    guid: str
    state: str                     # inProgress | completed | canceled
    file_path: str | None = None


@dataclass(slots=True)
class FrameScope:
    '''A cross-origin child frame that has its own scope: its frame id, its
    current URL, and the :class:`Driver` that drives it.'''
    frame_id: str
    url: str
    driver: typing.Any


#: Event kinds a stream can subscribe to (``Driver.listen(Console, Load)``).
EventKind = type


class Stream(typing.Protocol):
    '''What ``Driver.listen`` returns: an async iterator of normalized events
    that can be closed, and is usable as a context manager.'''

    def close(self) -> None: ...
    def __aiter__(self) -> typing.AsyncIterator[typing.Any]: ...
    def __enter__(self) -> Stream: ...
    def __exit__(self, *exc_info: object) -> None: ...


class Driver(typing.Protocol):
    '''The per-scope browser surface the testing layer is written against.

    Scripting: ``evaluate``/``call`` return JSON values and raise
    ``JSError`` on page exceptions; ``evaluate_handle``/``call_handle`` return
    a :class:`Handle` (or None when the result is not an object). ``call``
    args may be JSON data, Handles, or Elements (unwrapped to their handle).
    '''

    backend: str

    # -- liveness / setup --
    @property
    def alive(self) -> bool: ...
    async def arm(self, *, capture: bool, track_network: bool) -> None: ...

    # -- scripting --
    async def evaluate(self, expression: str, *, await_promise: bool = True,
                       user_gesture: bool = False) -> typing.Any: ...
    async def evaluate_handle(self, expression: str, *,
                              await_promise: bool = True,
                              user_gesture: bool = False) -> Handle | None: ...
    async def call(self, function: str, args: typing.Sequence[typing.Any], *,
                   await_promise: bool = True, user_gesture: bool = False
                   ) -> typing.Any: ...
    async def call_handle(self, function: str, args: typing.Sequence[typing.Any],
                          *, await_promise: bool = True,
                          user_gesture: bool = False) -> Handle | None: ...
    async def release(self, handle: Handle) -> None: ...
    async def array_handles(self, handle: Handle) -> list[Handle]: ...
    async def add_init_script(self, source: str) -> None: ...

    # -- nodes --
    async def node_frame_id(self, handle: Handle) -> str | None: ...
    async def set_files(self, handle: Handle, paths: typing.Sequence[str]) -> None: ...

    # -- navigation / content --
    async def navigate(self, url: str) -> tuple[str | None, str | None]: ...
    async def main_frame_id(self) -> str: ...
    async def set_content(self, html: str) -> None: ...

    # -- capture --
    async def screenshot(self, *, format: str = 'png', quality: int | None = None,
                         clip: dict | None = None,
                         beyond_viewport: bool = False) -> bytes: ...
    async def pdf(self, **options: typing.Any) -> bytes: ...

    # -- input (trusted) --
    async def mouse(self, kind: str, x: float, y: float, *,
                    button: str | None = None, click_count: int | None = None,
                    delta_x: float | None = None, delta_y: float | None = None
                    ) -> None: ...
    async def key(self, kind: str, *, key: str, code: str | None = None,
                  text: str | None = None, virtual_key: int | None = None
                  ) -> None: ...
    async def insert_text(self, text: str) -> None: ...

    # -- dialogs --
    async def handle_dialog(self, *, accept: bool,
                            prompt_text: str | None = None) -> None: ...

    # -- network --
    async def enable_network(self) -> None: ...
    async def collect_bodies(self) -> None: ...   # arm body capture (BiDi: a data collector)
    async def response_body(self, request_id: str) -> bytes: ...
    async def request_body(self, request_id: str) -> bytes | None: ...  # when not on the event
    async def cookies(self, urls: typing.Sequence[str] | None = None
                      ) -> list[dict]: ...
    async def set_cookies(self, cookies: typing.Sequence[dict]) -> None: ...
    async def clear_cookies(self) -> None: ...
    async def extra_headers(self, headers: dict[str, str]) -> None: ...

    # -- interception --
    async def enable_intercept(self) -> None: ...
    async def fulfill(self, request_id: str, *, status: int,
                      headers: typing.Sequence[tuple[str, str]],
                      body: bytes) -> None: ...
    async def continue_(self, request_id: str, *, url: str | None = None,
                        method: str | None = None,
                        post_data: bytes | None = None,
                        headers: typing.Sequence[tuple[str, str]] | None = None
                        ) -> None: ...
    async def fail(self, request_id: str, reason: str) -> None: ...

    # -- emulation (each may raise NotSupported) --
    async def viewport(self, width: int, height: int, *, scale: float = 1.0,
                       mobile: bool = False) -> None: ...
    async def user_agent(self, user_agent: str, *,
                         accept_language: str | None = None,
                         platform: str | None = None,
                         metadata: dict | None = None) -> None: ...
    async def media(self, *, media: str | None = None,
                    features: typing.Sequence[tuple[str, str]] = ()) -> None: ...
    async def geolocation(self, latitude: float, longitude: float, *,
                          accuracy: float = 1.0) -> None: ...
    async def timezone(self, timezone_id: str) -> None: ...

    # -- downloads --
    async def download_behavior(self, directory: str) -> None: ...

    # -- accessibility --
    # engine: 'native' = the browser's tree (CDP Accessibility domain; the only
    # option that is really native), 'walker' = the in-page walker (_ax_walker;
    # what BiDi always uses). Refs from a walker tree index its registry, so
    # resolve_ax_node needs the same engine the tree came from.
    async def ax_tree(self, *, depth: int | None = None,
                      engine: str = 'native') -> list[dict]: ...
    async def resolve_ax_node(self, backend_id: int, *,
                              engine: str = 'native') -> Handle | None: ...

    # -- cross-origin frames --
    async def enable_frame_attach(self) -> None: ...
    def frame_scopes(self) -> list[FrameScope]: ...
    async def frame_owner(self, frame_id: str) -> int | None: ...

    # -- events --
    def listen(self, *kinds: EventKind, buffer_size: int = 256) -> Stream: ...
    async def wait_for(self, *kinds: EventKind) -> typing.Any: ...

    # -- lifecycle --
    async def close_target(self) -> None: ...
