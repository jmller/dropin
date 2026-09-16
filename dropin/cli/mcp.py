"""Serve MCP with no other stdout and no eager engine/store construction."""

from ..mcp.server import serve


def run(context, args):
    return serve(context.config)
