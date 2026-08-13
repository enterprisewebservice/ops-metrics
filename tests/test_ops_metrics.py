import ast
from pathlib import Path

def test_server_contract_surface():
    source = Path("app/server.py").read_text()
    names = {node.name for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"weekly_summary", "stuck_orders", "top_products", "healthz", "summary", "stuck", "products"} <= names
    assert 'mcp.run(transport="streamable-http")' in source
    assert "http://orders-api.agent-office.svc.cluster.local:8080" in source
