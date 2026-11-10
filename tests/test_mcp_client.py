import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FIXTURE = Path(__file__).parent / "fixtures" / "sample.csv"


@pytest.mark.asyncio
async def test_stdio_server_roundtrip(tmp_path):
    env = {
        **os.environ,
        "TESOURO_DIRETO_CSV_URL": str(FIXTURE),
        "TESOURO_DIRETO_CACHE_DIR": str(tmp_path),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tesourodireto_mcp"],
        env=env,
        cwd=str(Path(__file__).parents[1]),
    )

    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {"list_bond_types", "get_bond_data", "get_current_rates"} <= names
        assert {"get_bond_stats", "get_yield_curve"} <= names

        result = await session.call_tool("list_bond_types", {})
        text = result.content[0].text
        assert "NTN-B Principal" in text

        result = await session.call_tool(
            "get_bond_data",
            {"asset_codes": ["LTN"], "first_year": 2024, "last_year": 2024},
        )
        text = result.content[0].text
        assert "LTN 010125" in text
        assert "2024-01-02" in text

        result = await session.call_tool(
            "get_current_rates", {"asset_codes": ["NTN-B Principal"]}
        )
        text = result.content[0].text
        assert "NTN-B Principal 150835" in text

        prompts = await session.list_prompts()
        assert any(prompt.name == "analyze_bond" for prompt in prompts.prompts)

        resources = await session.list_resources()
        assert any(
            str(resource.uri) == "tesouro://catalog/bond-types"
            for resource in resources.resources
        )
