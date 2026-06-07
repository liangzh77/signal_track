# 行情 CSV 导入

`import-bars` 用于把 licensed provider、券商或手工导出的日线 CSV 写入 SQLite。
这适合中国期货等暂时不能通过当前 provider 自动拉取的市场。

## 示例命令

```powershell
python -m signal_track.cli import-bars 铜主连 --market CN_FUT --file examples/cu-bars.csv --provider licensed-csv
```

导入后，`daily-run --provider none` 也可以使用这些本地价格数据计算曲线和检查规则。

## 支持列

必填列：

- `date`、`bar_date` 或 `trade_date`。
- `close`、`收盘` 或 `收盘价`。

可选列：

- `open`、`开盘`、`开盘价`。
- `high`、`最高`、`最高价`。
- `low`、`最低`、`最低价`。
- `adj_close`、`adjclose`、`复权收盘`。
- `volume`、`vol`、`成交量`。
- `amount`、`成交额`。
- `settle`、`结算价`。
- `open_interest`、`oi`、`持仓量`。

日期支持 `YYYY-MM-DD` 和 `YYYYMMDD`。数字列允许为空；`close` 不能为空。

## 示例 CSV

```csv
trade_date,open,high,low,close,vol,amount,settle,oi
20260601,80000,80500,79800,80300,1000,1200000,80200,50000
20260602,80300,81000,80100,80900,1100,1300000,80800,50500
```
