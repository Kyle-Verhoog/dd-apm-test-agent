import argparse
import base64
from collections import OrderedDict
import json
import logging
import os
import pprint
import sys
from typing import Awaitable
from typing import Callable
from typing import List
from typing import Literal
from typing import Optional
from typing import Set

from aiohttp import ClientSession
from aiohttp import web
from aiohttp.web import Request
from aiohttp.web import middleware

from . import _get_version
from . import trace_snapshot
from . import tracestats_snapshot
from .checks import CheckTrace
from .checks import Checks
from .checks import start_trace
from .trace import Trace
from .trace import TraceMap
from .trace import decode_v04 as trace_decode_v04
from .trace import decode_v05 as trace_decode_v05
from .trace import pprint_trace
from .trace import v04TracePayload
from .trace_checks import CheckMetaTracerVersionHeader
from .trace_checks import CheckTraceContentLength
from .trace_checks import CheckTraceCountHeader
from .tracestats import decode_v06 as tracestats_decode_v06
from .tracestats import v06StatsPayload


_Handler = Callable[[Request], Awaitable[web.Response]]


log = logging.getLogger(__name__)


def _parse_csv(s: str) -> List[str]:
    """Return the values of a csv string.

    >>> _parse_csv("a,b,c")
    ['a', 'b', 'c']
    >>> _parse_csv(" a, b ,c ")
    ['a', 'b', 'c']
    >>> _parse_csv(" a,b,c ")
    ['a', 'b', 'c']
    >>> _parse_csv(" a,")
    ['a']
    >>> _parse_csv("a, ")
    ['a']
    """
    return [s.strip() for s in s.split(",") if s.strip() != ""]


@middleware  # type: ignore
async def check_failure_middleware(request: Request, handler: _Handler) -> web.Response:
    """Convert any failed checks into an HttpException."""
    trace = start_trace("request %r" % request)
    try:
        response = await handler(request)
    except AssertionError as e:
        msg = str(trace) + str(e)
        log.error(msg)
        return web.HTTPBadRequest(body=msg)
    else:
        if trace.has_fails():
            msg = str(trace)
            log.error(msg)
            return web.HTTPBadRequest(body=msg)
    return response


def _session_token(request: Request) -> Optional[str]:
    token: Optional[str]
    if "X-Datadog-Test-Session-Token" in request.headers:
        token = request.headers["X-Datadog-Test-Session-Token"]
    elif "test_session_token" in request.url.query:
        token = request.url.query.get("test_session_token")
    else:
        token = None
    return token


@middleware  # type: ignore
async def session_token_middleware(request: Request, handler: _Handler) -> web.Response:
    """Extract session token from the request and store it in the request.

    The token is retrieved from the headers or params of the request.
    """
    token = _session_token(request)
    request["session_token"] = token
    return await handler(request)


class Agent:
    def __init__(self):
        """Only store the requests sent to the agent. There are many representations
        of data but typically information is lost while transforming the data.

        Storing exactly what is sent to the agent enables us to transform the data
        however we desire later on.
        """
        # Token to be used if running test cases synchronously
        self._requests: List[Request] = []

    async def traces(self) -> TraceMap:
        """Return the traces stored by the agent in the order in which they
        arrived.

        Spans from trace chunks are aggregated by trace id and returned as
        complete lists.
        """
        _traces: TraceMap = OrderedDict()
        for req in reversed(self._requests):
            traces = await self._decode_v04_traces(req)
            for t in traces:
                for s in t:
                    trace_id = s["trace_id"]
                    if trace_id not in _traces:
                        _traces[trace_id] = []
                    _traces[trace_id].append(s)
        return _traces

    async def _trace_by_trace_id(self, trace_id: int) -> Trace:
        return (await self.traces())[trace_id]

    def _requests_by_session(self, token: Optional[str]) -> List[Request]:
        """Return the latest requests sent with the given token.

        All requests since the most recent /session/start request are included.

        If no /session/start is given for the token then all requests made with
        the token are returned.
        """
        # Go backwards in the requests received gathering requests until
        # the /session-start request for the token is found.
        reqs: List[Request] = []
        for req in reversed(self._requests):
            if req.match_info.handler == self.handle_session_start:
                if token is None or _session_token(req) == token:
                    break
                else:
                    # The requests made were from a different manual session
                    # so continue.
                    continue
            if _session_token(req) in [token, None]:
                reqs.append(req)
        return reqs

    async def _traces_by_session(self, token: Optional[str]) -> List[Trace]:
        """Return the traces that belong to the given session token.

        If token is None or if the token was used to manually start a session
        with /session-start then return all traces that were sent since the last
        /session-start request was made.

        Spans are aggregated by trace_id (no ordering is performed).
        """
        tracemap: TraceMap = OrderedDict()
        for req in self._requests_by_session(token):
            if req.match_info.handler == self.handle_v04_traces:
                for trace in await self._decode_v04_traces(req):
                    for span in trace:
                        trace_id = span["trace_id"]
                        if trace_id not in tracemap:
                            tracemap[trace_id] = []
                        tracemap[trace_id].append(span)
        return list(tracemap.values())

    async def _tracestats_by_session(
        self, token: Optional[str]
    ) -> List[v06StatsPayload]:
        stats: List[v06StatsPayload] = []
        for req in self._requests_by_session(token):
            if req.match_info.handler == self.handle_v06_tracestats:
                s = await self._decode_v06_tracestats(req)
                stats.append(s)
        return stats

    async def _decode_v04_traces(self, request: Request) -> v04TracePayload:
        content_type = request.content_type
        raw_data = await request.read()
        return trace_decode_v04(content_type, raw_data)

    async def _decode_v05_traces(self, request: Request) -> v04TracePayload:
        raw_data = await request.read()
        return trace_decode_v05(raw_data)

    async def _decode_v06_tracestats(self, request: Request) -> v06StatsPayload:
        raw_data = await request.read()
        return tracestats_decode_v06(raw_data)

    async def handle_v04_traces(self, request: Request) -> web.Response:
        return await self._handle_traces(request, version="v0.4")

    async def handle_v05_traces(self, request: Request) -> web.Response:
        return await self._handle_traces(request, version="v0.5")

    async def handle_v06_tracestats(self, request: Request) -> web.Response:
        self._requests.append(request)
        stats = await self._decode_v06_tracestats(request)
        nstats = len(stats["Stats"])
        log.info(
            "received /v0.6/stats payload with %r stats bucket%s",
            nstats,
            "s" if nstats else "",
        )
        return web.HTTPOk()

    async def _handle_traces(
        self, request: Request, version: Literal["v0.4", "v0.5"]
    ) -> web.Response:
        self._requests.append(request)
        checks: Checks = request.app["checks"]

        with CheckTrace.add_frame("headers") as f:
            f.add_item(pprint.pformat(dict(request.headers)))
            checks.check("meta_tracer_version_header", headers=dict(request.headers))
            checks.check(
                "trace_content_length",
                content_length=int(request.headers["Content-Length"]),
            )
            if version == "v0.4":
                traces = await self._decode_v04_traces(request)
            elif version == "v0.5":
                traces = await self._decode_v05_traces(request)
            log.info("received trace payload with %r trace chunks", len(traces))
            for i, trace in enumerate(traces):
                try:
                    log.info(
                        "Chunk %d\n%s",
                        i,
                        pprint_trace(trace, request.app["log_span_fmt"]),
                    )
                except ValueError:
                    log.info(
                        "Chunk %d could not be displayed (might be incomplete).", i
                    )
            log.info("end of payload %s", "-" * 40)

            with CheckTrace.add_frame(f"payload ({len(traces)} traces)"):
                checks.check(
                    "trace_count_header",
                    headers=dict(request.headers),
                    num_traces=len(traces),
                )

        agent_url = request.app["agent_url"]
        if agent_url:
            log.info("Forwarding request to agent at %r", agent_url)
            async with ClientSession() as session:
                async with session.post(
                    f"{agent_url}/v0.4/traces",
                    headers=request.headers,
                    data=await request.read(),
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    log.info("Got response %r from agent", data)
                    return web.json_response(data=data)

        # TODO: implement sampling logic
        return web.json_response(data={"rate_by_service": {}})

    async def handle_session_start(self, request: Request) -> web.Response:
        self._requests.append(request)
        return web.HTTPOk()

    async def handle_snapshot(self, request: Request) -> web.Response:
        """Generate a snapshot or perform a snapshot test."""
        token = request["session_token"]
        snap_dir = request.url.query.get("dir", request.app["snapshot_dir"])
        snap_ci_mode = request.app["snapshot_ci_mode"]
        log.info(
            "performing snapshot with token=%r, ci_mode=%r and snapshot directory=%r",
            token,
            snap_ci_mode,
            snap_dir,
        )

        # Get the span attributes that are to be ignored for this snapshot.
        default_span_ignores: Set[str] = request.app["snapshot_ignored_attrs"]
        overrides = set(_parse_csv(request.url.query.get("ignores", "")))
        span_ignores = list(default_span_ignores | overrides)
        log.info("using ignores %r", span_ignores)

        with CheckTrace.add_frame(f"snapshot (token='{token}')") as frame:
            frame.add_item(f"Directory: {snap_dir}")
            frame.add_item(f"CI mode: {snap_ci_mode}")

            if "X-Datadog-Test-Snapshot-Filename" in request.headers:
                snap_file = request.headers["X-Datadog-Test-Snapshot-Filename"]
            elif "file" in request.url.query:
                snap_file = request.url.query.get("file")
            else:
                snap_file = os.path.join(snap_dir, token)

            # The logic from here is mostly duplicated for traces and trace stats.
            # If another data type is to be snapshotted then it probably makes sense to abstract away
            # the required pieces of snapshotting (loading, generating and comparing).

            # For backwards compatibility traces don't have a postfix of `_trace.json`
            trace_snap_file = f"{snap_file}.json"
            tracestats_snap_file = f"{snap_file}_tracestats.json"

            frame.add_item(f"Trace File: {trace_snap_file}")
            frame.add_item(f"Stats File: {tracestats_snap_file}")
            log.info(
                "using snapshot files %r and %r", trace_snap_file, tracestats_snap_file
            )

            trace_snap_path_exists = os.path.exists(trace_snap_file)

            received_traces = await self._traces_by_session(token)
            if snap_ci_mode and received_traces and not trace_snap_path_exists:
                raise AssertionError(
                    f"Trace snapshot file '{trace_snap_file}' not found. "
                    "Perhaps the file was not checked into source control? "
                    "The snapshot file is automatically generated when the test case is run when not in CI mode."
                )
            elif trace_snap_path_exists:
                # Do the snapshot comparison
                with open(trace_snap_file, mode="r") as f:
                    raw_snapshot = json.load(f)
                trace_snapshot.snapshot(
                    expected_traces=raw_snapshot,
                    received_traces=received_traces,
                    ignored=span_ignores,
                )
            elif received_traces:
                # Create a new snapshot for the data received
                with open(trace_snap_file, mode="w") as f:
                    f.write(trace_snapshot.generate_snapshot(received_traces))
                log.info(
                    "wrote new trace snapshot to %r", os.path.abspath(trace_snap_file)
                )

            # Get all stats buckets from the payloads since we don't care about the other fields (hostname, env, etc)
            # in the payload.
            received_stats = [
                bucket
                for p in (await self._tracestats_by_session(token))
                for bucket in p["Stats"]
            ]
            tracestats_snap_path_exists = os.path.exists(tracestats_snap_file)
            if snap_ci_mode and received_stats and not tracestats_snap_path_exists:
                raise AssertionError(
                    f"Trace stats snapshot file '{tracestats_snap_file}' not found. "
                    "Perhaps the file was not checked into source control? "
                    "The snapshot file is automatically generated when the test case is run when not in CI mode."
                )
            elif tracestats_snap_path_exists:
                # Do the snapshot comparison
                with open(tracestats_snap_file, mode="r") as f:
                    raw_snapshot = json.load(f)
                tracestats_snapshot.snapshot(
                    expected_stats=raw_snapshot,
                    received_stats=received_stats,
                )
            elif received_stats:
                # Create a new snapshot for the data received
                with open(tracestats_snap_file, mode="w") as f:
                    f.write(tracestats_snapshot.generate(received_stats))
                log.info(
                    "wrote new tracestats snapshot to %r",
                    os.path.abspath(tracestats_snap_file),
                )
        return web.HTTPOk()

    async def handle_session_traces(self, request: Request) -> web.Response:
        token = request["session_token"]
        traces = await self._traces_by_session(token)
        return web.json_response(traces)

    async def handle_session_tracestats(self, request: Request) -> web.Response:
        token = request["session_token"]
        stats = await self._tracestats_by_session(token)
        return web.json_response(stats)

    async def handle_session_requests(self, request: Request) -> web.Response:
        token = request["session_token"]
        resp = []
        for req in reversed(self._requests_by_session(token)):
            if req.match_info.handler not in (
                self.handle_v04_traces,
                self.handle_v05_traces,
                self.handle_v06_tracestats,
            ):
                continue
            resp.append(
                {
                    "headers": dict(req.headers),
                    "body": base64.b64encode(await req.read()).decode(),
                    "url": str(req.url),
                    "method": req.method,
                }
            )
        return web.json_response(resp)

    async def handle_test_traces(self, request: Request) -> web.Response:
        """Return requested traces as JSON.

        Traces can be requested by providing a header X-Datadog-Trace-Ids or
        a query param trace_ids.
        """
        raw_trace_ids = request.url.query.get(
            "trace_ids", request.headers.get("X-Datadog-Trace-Ids", "")
        )
        if raw_trace_ids:
            trace_ids = map(int, raw_trace_ids.split(","))
            traces = []
            for tid in trace_ids:
                try:
                    traces.append(await self._trace_by_trace_id(tid))
                except KeyError:
                    traces.append([])
        else:
            traces = list((await self.traces()).values())
        return web.json_response(data=traces)

    async def handle_session_clear(self, request: Request) -> web.Response:
        """Clear traces by session token or all traces if none is provided."""
        session_token = request["session_token"]
        if session_token is not None:
            # Clear any synchronous sessions.
            in_token_sync_session = False
            for req in self._requests:
                if req.match_info.handler == self.handle_session_start:
                    if _session_token(req) == session_token:
                        in_token_sync_session = True
                    else:
                        in_token_sync_session = False
                if in_token_sync_session:
                    setattr(req, "__delete", True)

            # Filter out all the requests.
            self._requests = [
                r
                for r in self._requests
                if _session_token(r) != session_token and not hasattr(r, "__delete")
            ]
        else:
            self._requests = []
        return web.HTTPOk()

    async def handle_trace_analyze(self, request: Request) -> web.Response:
        # client.get("/span/start")
        # client.get("/span/tag")
        # client.get("/span/finish")
        # wait 1s, gather traces and assert tags
        raise NotImplementedError


def make_app(
    disabled_checks: List[str],
    log_span_fmt: str,
    snapshot_dir: str,
    snapshot_ci_mode: bool,
    snapshot_ignored_attrs: List[str],
    agent_url: str,
) -> web.Application:
    agent = Agent()
    app = web.Application(
        client_max_size=int(100e6),  # 100MB - arbitrary
        middlewares=[
            check_failure_middleware,
            session_token_middleware,
        ],
    )
    app.add_routes(
        [
            web.post("/v0.4/traces", agent.handle_v04_traces),
            web.put("/v0.4/traces", agent.handle_v04_traces),
            web.post("/v0.5/traces", agent.handle_v05_traces),
            web.put("/v0.5/traces", agent.handle_v05_traces),
            web.put("/v0.6/stats", agent.handle_v06_tracestats),
            web.get("/test/session/start", agent.handle_session_start),
            web.get("/test/session/clear", agent.handle_session_clear),
            web.get("/test/session/snapshot", agent.handle_snapshot),
            web.get("/test/session/traces", agent.handle_session_traces),
            web.get("/test/session/stats", agent.handle_session_tracestats),
            web.get("/test/session/requests", agent.handle_session_requests),
            web.get("/test/traces", agent.handle_test_traces),
            # web.get("/test/benchmark", agent.handle_test_traces),
            web.get("/test/trace/analyze", agent.handle_trace_analyze),
        ]
    )
    checks = Checks(
        checks=[
            CheckMetaTracerVersionHeader,
            CheckTraceCountHeader,
            CheckTraceContentLength,
        ],
        disabled=disabled_checks,
    )
    app["checks"] = checks
    app["snapshot_dir"] = snapshot_dir
    app["snapshot_ci_mode"] = snapshot_ci_mode
    app["log_span_fmt"] = log_span_fmt
    app["snapshot_ignored_attrs"] = snapshot_ignored_attrs
    app["agent_url"] = agent_url
    return app


def main(args: Optional[List[str]] = None) -> None:
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Datadog APM test agent",
        prog="ddapm-test-agent",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="store_true",
        dest="version",
        help="Print version info and exit.",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=int(os.environ.get("PORT", 8126))
    )
    parser.add_argument(
        "--snapshot-dir",
        type=str,
        default=os.environ.get("SNAPSHOT_DIR", "snapshots"),
        help="Directory to store snapshots.",
    )
    parser.add_argument(
        "--snapshot-ci-mode",
        type=int,
        default=int(os.environ.get("SNAPSHOT_CI", 0)),
        help="Enable CI mode for snapshotting. Enforces that snapshot files exist.",
    )
    parser.add_argument(
        "--snapshot-ignored-attrs",
        type=Set[str],
        default=set(
            _parse_csv(
                os.environ.get(
                    "SNAPSHOT_IGNORED_ATTRS", trace_snapshot.DEFAULT_SNAPSHOT_IGNORES
                )
            )
        ),
        help=(
            "Comma-separated values of span attributes to ignore. "
            "meta/metrics attributes can be ignored by prefixing the key "
            "with meta. or metrics."
        ),
    )
    parser.add_argument(
        "--disabled-checks",
        type=List[str],
        default=_parse_csv(os.environ.get("DISABLED_CHECKS", "")),
        help=(
            "Comma-separated values of checks to disable. None are disabled "
            " by default. For the list of values see "
            "https://github.com/datadog/dd-trace-test-agent"
        ),
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Set the log level. DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    )
    parser.add_argument(
        "--log-span-fmt",
        type=str,
        default=os.environ.get("LOG_SPAN_FMT", "[{name}]"),
        help=(
            "Format to use when logging spans. Default is '[{name}]'. "
            "All span attributes are available."
        ),
    )
    parser.add_argument(
        "--agent-url",
        type=str,
        default=os.environ.get(
            "DD_TRACE_AGENT_URL", os.environ.get("DD_AGENT_URL", "")
        ),
        help=(
            "Datadog agent URL. If provided, any received data will be forwarded "
            "to the agent."
        ),
    )
    parsed_args = parser.parse_args(args=args)
    logging.basicConfig(level=parsed_args.log_level)

    if parsed_args.version:
        print(_get_version())
        sys.exit(0)

    if not os.path.exists(parsed_args.snapshot_dir) or not os.access(
        parsed_args.snapshot_dir, os.W_OK | os.X_OK
    ):
        log.warning(
            "default snapshot directory %r does not exist or is not readable. Snapshotting will not work.",
            os.path.abspath(parsed_args.snapshot_dir),
        )
    app = make_app(
        disabled_checks=parsed_args.disabled_checks,
        log_span_fmt=parsed_args.log_span_fmt,
        snapshot_dir=parsed_args.snapshot_dir,
        snapshot_ci_mode=parsed_args.snapshot_ci_mode,
        snapshot_ignored_attrs=parsed_args.snapshot_ignored_attrs,
        agent_url=parsed_args.agent_url,
    )
    web.run_app(app, port=parsed_args.port)


if __name__ == "__main__":
    main()
