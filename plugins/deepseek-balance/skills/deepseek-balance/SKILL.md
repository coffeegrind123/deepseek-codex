---
name: deepseek-balance
description: Check the DeepSeek API USD balance and account availability. Use when the user asks how much DeepSeek credit remains, requests a balance refresh, or needs a status suitable for a repo title or dashboard.
---

# DeepSeek Balance

Use the bundled MCP tool `get_deepseek_balance` when the user asks for the current DeepSeek API balance.

The server reads `DEEPSEEK_API_KEY` from its environment. Never ask the user to paste the key into chat, and never repeat, print, log, or include the key in a tool result.

Report the USD values returned by the tool:

- `total_usd`
- `granted_usd`
- `topped_up_usd`
- `available`

If the tool returns `USD_BALANCE_NOT_FOUND`, say that the DeepSeek response did not contain a USD balance entry. Do not report the CNY balance as zero USD.

If the tool reports `HTTP_ERROR`, `INVALID_JSON`, `INVALID_RESPONSE`, or `NETWORK_ERROR`, relay the safe error message and code. Do not retry with a modified endpoint or infer a balance from partial data.

For status-only requests, the formatter summary is sufficient. Include the component values when the user asks for a breakdown.
