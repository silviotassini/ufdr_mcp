"""UFDR MCP — servidor MCP para relatórios UFDR (Cellebrite)."""

from ufdr_parser import iter_report, model_to_dict

from ufdr_mcp.index import UFDRIndex, get_index

__all__ = ["UFDRIndex", "get_index", "iter_report", "model_to_dict"]
