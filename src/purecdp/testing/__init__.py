'''Layer 4: testing utilities — opinionated helpers for driving a browser.

- Page: goto/evaluate/wait/screenshot/console-capture/interception over a Session
- CDPTestCase: unittest base class (fresh browser + context + Page per test)
- pytest_plugin: optional pytest fixtures (pytest is never a dependency;
  import purecdp.testing.pytest_plugin only happens when pytest loads it)
'''

from .artifacts import dump_artifacts
from .element import ActionabilityError, Element
from .frame import Frame, FrameNotFound
from .live import ExpectationError, Live
from .human import HumanCursor, human_scroll, human_type
from .intercept import InterceptedRequest
from .page import (
    ConsoleMessage,
    Download,
    DownloadError,
    JSError,
    NavigateError,
    Page,
    launched_page,
)
from .recorder import Exchange, NetworkRecorder, to_har
from .snapshot import Snapshot, build_snapshot
from .sse import ServerSentEvent, parse_sse
from .stealth import (
    DEFAULT_EVASIONS,
    EVASIONS,
    OPT_IN_EVASIONS,
    apply_stealth,
    build_stealth_script,
)
from .testcase import CDPTestCase

__all__ = [
    'Page',
    'launched_page',
    'apply_stealth',
    'build_stealth_script',
    'EVASIONS',
    'DEFAULT_EVASIONS',
    'OPT_IN_EVASIONS',
    'HumanCursor',
    'human_type',
    'human_scroll',
    'Element',
    'ActionabilityError',
    'Live',
    'ExpectationError',
    'Frame',
    'FrameNotFound',
    'JSError',
    'NavigateError',
    'ConsoleMessage',
    'Download',
    'DownloadError',
    'InterceptedRequest',
    'NetworkRecorder',
    'Exchange',
    'to_har',
    'Snapshot',
    'build_snapshot',
    'ServerSentEvent',
    'parse_sse',
    'CDPTestCase',
    'dump_artifacts',
]
