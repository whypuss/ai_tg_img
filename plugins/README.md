# plugins/ 使用說明

本目錄對應 JAVBUS JSON 插件 v1 協議。

- 每個 `.json` 是一個搜索源，`id` 全局唯一
- `enabled: false` 的插件會被跳過
- 新增/修改插件後無需重啟，`/bts` 下次搜索自動載入

## 快速開始

1. 參考 `example-html.json`（HTML 正則）或 `example-api.json`（JSON API）
2. 修改 `baseUrl`、`search.url`、`search.itemPattern`/`itemsPath`、`fields` 等
3. 啟用：`"enabled": true`
4. 測試：`/bts 關鍵詞`

## 完整協議

見 `https://github.com/WEP-56/JAVBUS/blob/main/docs/json_plugin_v1.md`

## 常用模板變量

- `{query}` 已 URL 編碼, `{queryRaw}` 原始, `{queryBase64}` URL-safe Base64
- `{page}` 1 開始, `{page0}` 0 開始
- `{sourceItemId}`, `{infoHash}` 等用於 detail/defaults

## 發布頁（防域名更換）

啟用 `announcement.enabled: true` 並配置 `url`/`urlPattern`，引擎會自動解析最新 `baseUrl`（含多跳 `steps`）。

## 從 URL 安裝

```bash
curl -o plugins/my-source.json https://example.com/path/to/plugin.json
```
