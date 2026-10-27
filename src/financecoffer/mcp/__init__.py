"""MCP surface: three read-only tools over the shared query service.

The package deliberately does not import the MCP SDK at module load time so the
``fcoffer mcp tools`` catalogue works without the optional ``mcp`` extra.
"""
