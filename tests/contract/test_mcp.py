"""Newline JSON-RPC against real stdin/stdout and fail-fast seams."""

import contextlib
import io
import json
from unittest.mock import patch

from tests.query_support import QueryTestCase


def request(method, params=None, id=1):
    result = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        result["params"] = params
    return result


class McpTest(QueryTestCase):
    def wire(self, *messages):
        data = b"\n".join(m.encode() if isinstance(m, str) else json.dumps(m).encode() for m in messages) + b"\n"
        result = self.cli("mcp", stdin=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        return [json.loads(line) for line in result.stdout.splitlines()]

    def call(self, name, arguments, **params):
        [response] = self.wire(request("tools/call", {"name": name, "arguments": arguments, **params}))
        return response

    def payload(self, response):
        result = response["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["content"][0]["type"], "text")
        return json.loads(result["content"][0]["text"])

    def test_lifecycle_tools_schemas_stdout_purity_and_eof(self):
        responses = self.wire(request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                     "clientInfo": {"name": "test", "version": "1"}}),
                              {"jsonrpc": "2.0", "method": "notifications/initialized"},
                              request("ping", id="ping"), request("tools/list", id=3))
        self.assertEqual(len(responses), 3)
        initialized = responses[0]["result"]
        self.assertEqual(initialized["protocolVersion"], "2025-06-18")
        self.assertEqual(initialized["capabilities"], {"tools": {}})
        self.assertEqual(initialized["serverInfo"]["name"], "dropin")
        self.assertTrue(initialized["serverInfo"]["version"])
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": "ping", "result": {}})
        tools = {t["name"]: t for t in responses[2]["result"]["tools"]}
        self.assertEqual(set(tools), {"find", "show", "get"})
        for tool in tools.values():
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            # Keep the public schemas in the conservative subset understood by
            # Codex CLI; cross-field constraints remain server-side checks.
            self.assertNotIn("allOf", tool["inputSchema"])
            self.assertNotIn("oneOf", tool["inputSchema"])
        properties = tools["find"]["inputSchema"]["properties"]
        self.assertEqual(properties["limit"]["default"], 100)
        self.assertEqual(properties["include_attributes"]["type"], "boolean")
        self.assertEqual(properties["tags"]["items"]["type"], "string")
        self.assertEqual(self.cli("mcp", stdin=b"").returncode, 0)

    def test_codex_reserved_call_metadata_is_ignored(self):
        # Codex 0.154 sends this MCP-standard reserved field on every call.
        meta = {"callId": "exec-123", "progressToken": 1,
                "x-codex-turn-metadata": {"thread_id": "thread-123"}}
        result = self.payload(self.call("find", {"name": "Tax-March"}, _meta=meta))
        self.assertEqual([record["archive_path"] for record in result],
                         [self.paths["Tax-March.PDF"]])

    def test_find_compact_and_include_attributes_exactly_match_show(self):
        summary = self.payload(self.call("find", {"name": "Tax", "tags": ["tax", "work"],
                                                  "kind": "com.adobe.pdf", "created_until": "2026-03-31"}))
        self.assertEqual([r["archive_path"] for r in summary], [self.paths["Tax-March.PDF"]])
        self.assertNotIn("attributes", summary[0])
        self.assertNotIn("attribute_values", summary[0])
        detailed = self.payload(self.call("find", {"name": "Tax-March", "include_attributes": True}))[0]
        shown = self.payload(self.call("show", {"archive_path": self.paths["Tax-March.PDF"]}))
        for key in ("attributes", "attribute_values"):
            self.assertEqual(detailed[key], shown[key])
        self.assertEqual(self.payload(self.call("show", {"sha256": self.hashes["Tax-March.PDF"]})), [shown])
        self.assertEqual(self.payload(self.call("show", {"sha256": "f" * 64})), [])

    def test_state_scope_excludes_prepublication_but_keeps_gate_d_abandoned(self):
        for state in ("recorded", "transferred", "verified", "abandoned"):
            self.seed("pre-" + state, state=state, confirmed=False)
            self.assertTrue(self.call("show", {"archive_path": self.paths["pre-" + state]})["result"]["isError"])
        for state in ("recoverable", "evicting", "evicted", "abandoned"):
            self.seed("post-" + state, state=state)
        self.assertEqual(self.payload(self.call("find", {"name": "pre-"})), [])
        self.assertEqual(len(self.payload(self.call("find", {"name": "post-"}))), 4)

    def test_protocol_errors_and_parameter_errors_do_not_kill_server(self):
        messages = ["{bad", request("no-such-method"), request("tools/call", []),
                    request("tools/call", {"name": "find", "arguments": {"typo": 1}}),
                    request("tools/call", {"name": "find", "arguments": {"include_attributes": "true"}}),
                    request("tools/call", {"name": "find", "arguments": {"tags": "tax"}}),
                    request("tools/call", {"name": "find", "arguments": {"limit": True}}),
                    request("tools/call", {"name": "find", "arguments": {"created_until": "bad"}}),
                    request("tools/call", {"name": "find", "arguments": {"text": '"bad'}}),
                    request("tools/call", {"name": "show", "arguments": {}}),
                    request("tools/call", {"name": "show", "arguments": {"archive_path": "a", "sha256": "f" * 64}}),
                    request("tools/call", {"name": "get", "arguments": {"archive_path": "a", "destination_dir": "/tmp", "force": 1}})]
        responses = self.wire(*messages, request("ping", id=99))
        self.assertEqual([r["error"]["code"] for r in responses[:-1]], [-32700, -32601] + [-32602] * 10)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[-1]["result"], {})

    def test_nested_json_parse_failure_does_not_kill_server(self):
        responses = self.wire("[" * 1100, request("ping", id=99))
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 99, "result": {}})

    def test_overflowing_timezone_offsets_are_invalid_params(self):
        for offset in ("+00:60", "-00:60", "+01:99", "-01:99", "+24:00"):
            with self.subTest(offset=offset):
                response = self.call("find", {"created_since": "2026-03-01T00:00:00" + offset})
                self.assertEqual(response["error"]["code"], -32602)

    def test_invalid_request_objects_notifications_and_unknown_tool(self):
        responses = self.wire("[]", "null", "42", {"jsonrpc": "1.0", "id": 1, "method": "ping"},
                              {"jsonrpc": "2.0", "id": [], "method": "ping"},
                              {"jsonrpc": "2.0", "method": "unknown-notification"}, request("ping", id=2))
        self.assertEqual([r["error"]["code"] for r in responses[:-1]], [-32600] * 5)
        self.assertEqual(responses[-1]["id"], 2)
        self.assertEqual(self.call("unknown", {})["error"]["code"], -32602)

    def test_unknown_query_and_retrieval_paths_are_tool_errors(self):
        for name, args, kind in (("show", {"archive_path": "no/such/path"}, "missing"),
                                 ("get", {"archive_path": "anything", "destination_dir": str(self.temp.root / "out")}, "missing")):
            response = self.call(name, args)["result"]
            self.assertTrue(response["isError"])
            payload = json.loads(response["content"][0]["text"])
            self.assertEqual(payload["error"], kind)
            self.assertTrue(payload["reason"])
        self.assertFalse((self.temp.root / "out").exists())

    def test_show_invalid_params_are_validated_before_opening_store(self):
        self.db.close()
        self.temp.store_path.unlink()
        for args in ({}, {"sha256": "bad"}, {"archive_path": "a", "sha256": "f" * 64}):
            with self.subTest(args=args):
                self.assertEqual(self.call("show", args)["error"]["code"], -32602)
        result = self.call("show", {"archive_path": "a"})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["error"], "store")

    def test_invalid_unicode_argument_is_invalid_params_not_server_crash(self):
        response = self.call("find", {"name": "invalid\ud800"})
        self.assertEqual(response["error"]["code"], -32602)

    def test_unknown_method_always_returns_method_not_found(self):
        [response] = self.wire(request("unknown-method", []))
        self.assertEqual(response["error"]["code"], -32601)

    def test_nonfinite_json_number_is_parse_error(self):
        [response] = self.wire('{"jsonrpc":"2.0","id":1e999,"method":"ping"}')
        self.assertEqual(response["error"]["code"], -32700)
        self.assertIsNone(response["id"])

    def test_queries_never_instantiate_engine_take_writer_lock_or_use_writable_context(self):
        from dropin.__main__ import main
        from dropin.cli import Context
        messages = [request("tools/call", {"name": "find", "arguments": {}}),
                    request("tools/call", {"name": "show", "arguments": {"archive_path": self.paths["Notes.txt"]}}),
                    request("tools/call", {"name": "get", "arguments": {"archive_path": "a", "destination_dir": "unused"}})]
        with patch.object(Context, "engine", property(lambda _: self.fail("engine"))), \
             patch.object(Context, "db", property(lambda _: self.fail("writable db"))), \
             patch("dropin.cli.tools_gate", side_effect=AssertionError("tools")), \
             patch("dropin.pipeline.writer_lock.writer_lock", side_effect=AssertionError("writer lock")), \
             patch("sys.stdin", io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--config", str(self.config_path), "mcp"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertFalse((self.temp.state_dir / "writer.lock").exists())


from tests.retrieve_support import RetrieveTestCase


class McpGetTest(RetrieveTestCase):
    def get_call(self, arguments):
        from dropin.__main__ import main
        from dropin.cli import Context
        messages = [request('tools/call', {'name': 'get', 'arguments': arguments}), request('ping', id=2)]
        before = list(self.db.iterdump())
        with patch.object(Context, 'engine', property(lambda _: self.engine)), \
             patch.object(Context, 'db', property(lambda _: self.fail('writable store'))), \
             patch('dropin.cli.tools_gate', return_value=None), \
             patch('dropin.pipeline.writer_lock.writer_lock', side_effect=AssertionError('lock')), \
             patch('sys.stdin', io.StringIO('\n'.join(map(json.dumps, messages)) + '\n')), \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(['--config', str(self.config_path), 'mcp']), 0)
        responses = list(map(json.loads, output.getvalue().splitlines()))
        self.assertEqual(responses[1]['result'], {})
        self.assertEqual(error.getvalue(), '')
        self.assertEqual(before, list(self.db.iterdump()))
        return responses[0]

    def test_get_verified_result_force_aside_and_corruption_tool_error(self):
        from pathlib import Path
        path, _, snapshot, archive = self.archived()
        args = {'archive_path': archive, 'destination_dir': str(self.out)}
        result = self.get_call(args)['result']
        self.assertFalse(result['isError'], result)
        payload = json.loads(result['content'][0]['text'])
        self.assertTrue(payload['verified'])
        self.assertEqual(payload['kind'], 'file')
        self.assertEqual(Path(payload['written_path']).read_bytes(), path.read_bytes())
        self.assertTrue(self.get_call(args)['result']['isError'])
        result = self.get_call({**args, 'force': True})['result']
        self.assertFalse(result['isError'])
        self.assertTrue(Path(json.loads(result['content'][0]['text'])['aside_path']).exists())
        self.engine.inject_corruption(snapshot, str(path), flip_byte=True)
        result = self.get_call({**args, 'force': True})['result']
        self.assertTrue(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'corrupt')
        self.assertEqual((self.out / path.name).read_bytes(), path.read_bytes())
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])

    def test_darwin_mcp_get_accepts_arbitrary_destination_and_force(self):
        import os
        from pathlib import Path
        from dropin import retrieve as retrieve_module
        from dropin.retrieve_acquire import sys as acquire_sys

        path, _, _, archive = self.archived('Mac.app', tree=True, bundle=True)
        def move(source_fd, source, destination_fd, destination):
            os.rename(source, destination, src_dir_fd=source_fd,
                      dst_dir_fd=destination_fd)
        args = {'archive_path': archive, 'destination_dir': str(self.out)}
        with patch.object(acquire_sys, 'platform', 'darwin'), \
             patch.object(retrieve_module, 'rename_noreplace', move):
            result = self.get_call(args)['result']
        self.assertFalse(result['isError'], result)
        payload = json.loads(result['content'][0]['text'])
        self.assertTrue(payload['verified'])
        target = self.out / path.name
        self.assertEqual((target / 'sub/a').read_bytes(), b'verified bytes')
        (target / 'marker').write_bytes(b'existing')
        with patch.object(acquire_sys, 'platform', 'darwin'), \
             patch.object(retrieve_module, 'rename_noreplace', move):
            result = self.get_call({**args, 'force': True})['result']
        self.assertFalse(result['isError'], result)
        payload = json.loads(result['content'][0]['text'])
        self.assertEqual(Path(payload['aside_path'], 'marker').read_bytes(), b'existing')

    def test_get_parameter_errors_are_rpc_errors(self):
        for args in ({}, {'archive_path': 'a'}, {'archive_path': 'a', 'destination_dir': 'b', 'force': 1}):
            self.assertEqual(self.get_call(args)['error']['code'], -32602)

    def test_get_remote_error_is_tool_error_and_session_survives(self):
        from dropin.engine.interface import EngineError
        _, _, _, archive = self.archived()
        with patch.object(self.engine, 'dump', side_effect=EngineError('no-repo', 'disposable offline fixture')):
            result = self.get_call({'archive_path': archive, 'destination_dir': str(self.out)})['result']
        self.assertTrue(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'no-repo')
        self.assertEqual(list(self.out.iterdir()), [])

    def test_cleanup_failure_is_retrieval_error_not_store_error(self):
        path, _, snapshot, archive = self.archived()
        self.engine.inject_corruption(snapshot, str(path), flip_byte=True)
        with patch('dropin.retrieve._cleanup', side_effect=PermissionError('cleanup denied')):
            result = self.get_call({'archive_path': archive, 'destination_dir': str(self.out)})['result']
        self.assertTrue(result['isError'])
        payload = json.loads(result['content'][0]['text'])
        self.assertEqual(payload['error'], 'corrupt')
        self.assertIn('hash', payload['reason'])
        self.assertIn('cleanup denied', payload['reason'])

    def test_mode000_collision_is_retrieval_refusal(self):
        from dropin import retrieve as module
        _, _, _, archive = self.archived('locked', tree=True, captured_dir_mode=0o40000)
        native = module.rename_noreplace
        def collide(*args):
            (self.out / 'locked').mkdir()
            return native(*args)
        with patch.object(module, 'rename_noreplace', collide):
            result = self.get_call({'archive_path': archive, 'destination_dir': str(self.out)})['result']
        self.assertTrue(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'refused')
        self.assertEqual(list(self.out.glob('.dropin-restore-*')), [])
