# MCP local baseline

Recorded: 2026-08-29  
Source ref: `main` at `4799d944e6f30cdb13ee752f2c7bba18b45ecfe2`  
Purpose: contract reference for the Yandex remote-MCP parity test. This baseline
describes the existing local implementation; it does not change its business logic.

## Runtime

- Package: `marketplaces-mcp-ru` v0.3.3
- Language: Python 3.10+
- MCP SDK constraint: `mcp>=1.2,<2`
- Current transport: stdio
- Local launch: `python serve.py all`
- Per-service launches: `python -m wb_mcp.server`,
  `python -m ozon_mcp.server`, `python -m ozon_perf_mcp.server`
- Combined entry point: `core.combined:main`
- Known-good offline check: `python serve.py all --selfcheck`
- No MCP resources and no MCP prompts are registered.

## Composition

The combined MCP is named `marketplaces-mcp-ru` and merges:

| Service | FastMCP name | Tools |
| --- | --- | ---: |
| Wildberries | `wb_mcp` | 21 |
| Ozon Seller | `ozon_mcp` | 21 |
| Ozon Performance | `ozon_perf_mcp` | 16 |
| **Combined** | `marketplaces-mcp-ru` | **58** |

## Tool contract

Each tool keeps the existing name, description and input signature unchanged in
the remote implementation. The parameters below are the input schema source
from the registered Python functions; optional parameters retain their current
defaults.

### WB

- `wb_check_auth()`
- `wb_list_sections()`
- `wb_get_section(section: str)`
- `wb_search_methods(query: str, limit: int = 15)`
- `wb_map(entity: str = "")`
- `wb_describe_method(operation_id: str)`
- `wb_call_method(operation_id: str, path_values: object = null, query: object = null, body: object = null, confirm_write: bool = false, i_understand_this_modifies_data: bool = false)`
- `wb_call_raw(method: str, path: str, host: str | null = null, query: object = null, body: object = null, confirm_write: bool = false, i_understand_this_modifies_data: bool = false)`
- `wb_fetch_all(operation_id: str, query: object = null, body: object = null, path_values: object = null, items_path: str | null = null, limit: int = 1000, max_items: int = 10000)`
- `wb_list_cabinets()`
- `wb_add_cabinet(credentials: object, name: str = "", i_understand_key_goes_to_chat: bool = false)`
- `wb_set_key(credentials: object, cabinet: str = "", i_understand_key_goes_to_chat: bool = false)`
- `wb_use_cabinet(name: str)`
- `wb_remove_cabinet(name: str)`
- `wb_list_workflows()`
- `wb_get_workflow(name: str)`
- `wb_get_sales(date_from: str, flag: int = 0)`
- `wb_get_stocks(date_from: str = "2020-01-01")`
- `wb_get_new_orders()`
- `wb_get_prices(limit: int = 1000, offset: int = 0, filter_nm_id: int | null = null)`
- `wb_set_price(nm_id: int, price: int, discount: int = 0, confirm_write: bool = false)`

### Ozon Seller

- `ozon_check_auth()`
- `ozon_list_sections()`
- `ozon_get_section(section: str)`
- `ozon_search_methods(query: str, limit: int = 15)`
- `ozon_map(entity: str = "")`
- `ozon_describe_method(operation_id: str)`
- `ozon_call_method(operation_id: str, path_values: object = null, query: object = null, body: object = null, confirm_write: bool = false, i_understand_this_modifies_data: bool = false)`
- `ozon_call_raw(method: str, path: str, host: str | null = null, query: object = null, body: object = null, confirm_write: bool = false, i_understand_this_modifies_data: bool = false)`
- `ozon_fetch_all(operation_id: str, query: object = null, body: object = null, path_values: object = null, items_path: str | null = null, limit: int = 1000, max_items: int = 10000)`
- `ozon_list_cabinets()`
- `ozon_add_cabinet(credentials: object, name: str = "", i_understand_key_goes_to_chat: bool = false)`
- `ozon_set_key(credentials: object, cabinet: str = "", i_understand_key_goes_to_chat: bool = false)`
- `ozon_use_cabinet(name: str)`
- `ozon_remove_cabinet(name: str)`
- `ozon_list_workflows()`
- `ozon_get_workflow(name: str)`
- `ozon_get_products(visibility: str = "ALL", limit: int = 100, last_id: str = "")`
- `ozon_get_stocks(visibility: str = "ALL", limit: int = 100, last_id: str = "")`
- `ozon_get_prices(visibility: str = "ALL", limit: int = 100, cursor: str = "")`
- `ozon_get_fbs_unfulfilled(cutoff_from: str, cutoff_to: str, limit: int = 100, offset: int = 0)`
- `ozon_set_price(offer_id: str, price: str, old_price: str = "0", min_price: str = "0", currency_code: str = "RUB", confirm_write: bool = false)`

### Ozon Performance

- `ozon_perf_check_auth()`
- `ozon_perf_list_sections()`
- `ozon_perf_get_section(section: str)`
- `ozon_perf_search_methods(query: str, limit: int = 15)`
- `ozon_perf_map(entity: str = "")`
- `ozon_perf_describe_method(operation_id: str)`
- `ozon_perf_call_method(operation_id: str, path_values: object = null, query: object = null, body: object = null, confirm_write: bool = false, i_understand_this_modifies_data: bool = false)`
- `ozon_perf_call_raw(method: str, path: str, host: str | null = null, query: object = null, body: object = null, confirm_write: bool = false, i_understand_this_modifies_data: bool = false)`
- `ozon_perf_fetch_all(operation_id: str, query: object = null, body: object = null, path_values: object = null, items_path: str | null = null, limit: int = 1000, max_items: int = 10000)`
- `ozon_perf_list_cabinets()`
- `ozon_perf_add_cabinet(credentials: object, name: str = "", i_understand_key_goes_to_chat: bool = false)`
- `ozon_perf_set_key(credentials: object, cabinet: str = "", i_understand_key_goes_to_chat: bool = false)`
- `ozon_perf_use_cabinet(name: str)`
- `ozon_perf_remove_cabinet(name: str)`
- `ozon_perf_list_workflows()`
- `ozon_perf_get_workflow(name: str)`

## Required configuration and dependencies

| Service | Environment variables | External dependencies |
| --- | --- | --- |
| WB | `WB_API_TOKEN` | Wildberries Seller API (`*.wildberries.ru`) |
| Ozon Seller | `OZON_CLIENT_ID`, `OZON_API_KEY` | Ozon Seller API (`*.ozon.ru`) |
| Ozon Performance | `OZON_PERF_CLIENT_ID`, `OZON_PERF_CLIENT_SECRET` | Ozon Performance API and OAuth token endpoint (`*.ozon.ru`) |

Credentials may alternatively resolve from the local
`~/.marketplace-mcp/cabinets.json` store. That store is local-only and must
not be copied into Docker, GitHub, Terraform variables or Yandex Lockbox
configuration files. The cloud implementation must inject only the listed
environment variables from Lockbox.

No database is used. Runtime input files are the versioned endpoint catalogs,
workflow YAML files and `core/entities.yaml`.

## Parity criteria

Before a cloud revision is accepted, compare local and remote `tools/list`
responses for the 58 names, descriptions and input schemas. A health endpoint,
if added for container orchestration, is not an MCP tool and must not affect
this contract.
