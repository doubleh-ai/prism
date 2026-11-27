import asyncio
from datetime import date

import httpx
import pytest

from server import warehouse_receipts as receipts

DAY = date(2026, 10, 30)


def sample():
    def table(title, number):
        return f"""
            <table class="el-table_table">
            <thead><tr><th>地区</th><th>仓库</th><th>期货</th><th>增减</th></tr></thead>
            <tr class="special_row_type"><td>{title}</td><td>单位：吨</td></tr>
            <tr><td>合计</td><td>99</td><td>3</td></tr>
            <tr><td colspan="2">总计</td><td>{number}</td><td>-2</td></tr></table>"""

    return (
        '<div id="stock"><table class="content_intro"><tr><td>2026-10-30 2026年 第198期</td></tr></table>'
        + table("铜(BC)", 123)
        + table("铜", 17)
        + "</div>"
    )


async def test_exact_exchange_total_uses_bar_date_and_separates_bonded_copper():
    def serve(request):
        assert str(request.url) == receipts.report_url(DAY)
        return httpx.Response(200, text=sample())

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await receipts.read_report(client, "CU", DAY, asyncio.get_running_loop().time() + 3)
    assert (result["status"], result["quantity"], result["change"], result["unit"]) == (
        "collected",
        17,
        -2,
        "吨",
    )
    assert result["data_as_of"] == DAY.isoformat() and result["source_url"].endswith("20261030/ZH/all.html")


@pytest.mark.parametrize(
    "before,after",
    [
        ("2026-10-30", "2026-10-29"),
        ("单位：吨", "单位：手"),
        ("总计", "合计"),
        ("期货", "总持仓"),
        ("铜(BC)", "铜"),
        ("<td>17</td>", "<td>-1</td>"),
    ],
)
def test_ambiguous_or_mismatched_public_data_is_never_used(before, after):
    with pytest.raises(ValueError):
        receipts.parse_report(sample().replace(before, after).encode(), "CU", DAY)


async def test_unsupported_exchange_sends_no_request():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: pytest.fail("unexpected fetch"))
    ) as client:
        assert await receipts.read_report(client, "UNSUPPORTED", DAY, 0) == {"status": "not_covered"}
