# 猫超补货 RPA

这个目录是猫超补货表数据源下载的独立 RPA 工程。

## 覆盖范围

本次只做：

- 1、实时库存
- 2、库存分析 - 品仓明细表
- 3、系统单
- 4、补货单列表
- 10、库位明细
- 11、调拨单

不做：

- 5、E3库存
- 6、库存锁定流水账
- 7、库存锁定单
- 8、出库流水账
- 9、WMS数据

## 结构

- `maochao_rpa.py`：主脚本
- `account_store.py`：加密账号数据库
- `config.example.json`：示例配置
- `downloads/`：账号独立下载目录
- `browser_profiles/`：账号独立浏览器资料目录
- `data/`：归档后的原始/清洗文件
- `logs/`：运行日志和 manifest

## 先做什么

1. 先初始化账号数据库：

```bash
python account_store.py init
```

2. 把 `config.example.json` 复制成 `config.local.json`，然后把真实账号导入数据库：

```bash
python account_store.py import-json config.local.json
```

3. 配好每个账号的：
   - `port`
   - `profile_dir`
   - `download_dir`
   - `supplier_names`
   - `xpath_vars`
   - `selector_overrides`

4. 核对 XPath。`10、库位明细` 当前按「商品 -> 渠道货品 -> 筛选 -> 导出」处理。

5. 如果要接手整合会话，优先先看 `MULTI_WINDOW_SYNC.md` 顶部的“会话整合快速检索”和 `10` 号注意事项，再决定是否重跑。

## 命令

检查配置：

```bash
python3 maochao_rpa.py dry-run --config config.local.json
```

首次登录：

```bash
python3 maochao_rpa.py login --config config.local.json --account tmall_inventory_01
```

顺序执行：

```bash
python3 maochao_rpa.py run --config config.local.json
```

只跑某几个任务：

```bash
python3 maochao_rpa.py run --config config.local.json --task 1 --task 4
```

## 说明

- 一个账号对应一个 Chrome 端口和 `user-data-dir`
- 初期建议顺序执行
- 下载后会自动做基础清洗，并输出 manifest
- 账号库里的密码会加密保存，不直接明文落地
- 如果同一页面在不同账号里文案不同，把差异放进 `xpath_vars`
- 如果整个 XPath 要按账号改写，把差异放进 `selector_overrides`
- 运行出错时会自动把截图保存到 `logs/screenshots/`

## 账号模板

`xpath_vars` 适合放文本变量，例如：

```json
{
  "supplier_name": "某供应商",
  "merchant_type_text": "商品供应商"
}
```

`selector_overrides` 适合按账号直接替换某个 selector，例如：

```json
{
  "realtime": {
    "supplier_first_option": "//li[contains(normalize-space(.), '{{supplier_name}}')]"
  }
}
```
