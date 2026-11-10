# 第一轮数据方法与复现

任务授权：项目负责人于2026-11-10明确要求完成黄金与10年期实际利率的第一轮事实整理，包括数据、图表和事实说明，不开展宏观归因。研究观察截止标签为2026-11-09，信息基准为2026-11-10 13:45（Asia/Shanghai）。

## 数据选择

优先使用已具备积分权限的Tushare：`fx_daily` 的 `XAUUSD.FXCM`、`us_trycr` 的 `y10`。前者按买卖收盘价中点计算，后者以百分数表示。接口按年分片获取，均未触及单次条数上限。5000积分账户的本次实测调用成功，不额外申请权限。

Tushare黄金日期标签的星期分布随年份变化，尚未确认标签对应的实际交易日；报告和图表均标记为初步版。处理程序保留原始日期，不凭价格或事件倒推日期。FRED DFII10仅用于利率交叉验证，不替代或拼接主序列。

## 本地文件与仓库文件

- 原始响应：`data/raw/2026-11-10/fx_daily_YYYY.json`、`us_trycr_YYYY.json`，以及 `fred_DFII10.csv`。
- 下载参数、行数、UTC获取时间及SHA-256：`data/metadata/snapshot_manifest.json`。
- 共同日标签样本：`data/processed/gold_realrate_common_daily.csv`。
- 全部周标签（含未完周）：`data/processed/gold_realrate_weekly_all.csv`。
- 完整周值及收益率、利率变化、滚动相关：`data/processed/gold_realrate_weekly.csv`。
- 利率校验差异：`data/processed/rate_reference_mismatches.csv`。
- 汇总检查：`outputs/quality_checks.json`。
- 研究说明：`outputs/findings.md`；五张PNG图位于 `outputs/figures/`。

原始及处理后序列留在本地并由.gitignore排除，再分发许可尚未核实。仓库保存代码、依赖、元数据、图表与汇总结果。仅从GitHub克隆不能获得原始数据；重新获取需要自己的有效Tushare凭证。即使重新获取成功，供应商的历史修订仍可能导致哈希或结果不同。

## 环境与执行

首次执行可在独立环境中安装依赖（本次使用现有Anaconda Python 3.12.7，未额外安装）：

```powershell
python -m pip install -r requirements.txt
```

有本地原始快照时，无需token即可重新计算全部结果：

```powershell
python -B src/gold_realrate.py
```

首次下载时，在PowerShell交互输入token，仅置于当前进程环境；不把真实token放入脚本或命令历史：

```powershell
$tokenInput = Read-Host 'Tushare token' -AsSecureString
$env:TUSHARE_TOKEN = [System.Net.NetworkCredential]::new('', $tokenInput).Password
try {
    python -B src/gold_realrate.py --fetch
} finally {
    Remove-Item Env:TUSHARE_TOKEN -ErrorAction SilentlyContinue
    $tokenInput.Dispose()
}
```

`--fetch`只下载缺失的年度文件，保留已存在快照，不会静默覆盖。基准日、样本上限及快照目录为本次任务固定配置；后续更新须明确新任务与截止时点，并保留本次快照。请求失败会停止，写入不含凭证的失败说明；不得将部分下载误当完整样本。

若FRED参考下载失败，主分析仍可进行，但质量文件标明交叉校验不可用。程序不会自动切换或拼接黄金源。探索性备选源访问失败情况已在findings.md说明。

## 计算约定

从2003-01-01起取两条序列原始日期标签的交集，只保留周一至周五。`W-FRI`按周六至周五分箱，每周选最后共同有效标签，记录对应的观测日期。保留空周及周频网格；不填充、不插值。首周可能不足一周，仅作为收益率起点。

完整周必须早于2026-11-10，最后完整周标签为2026-11-06。2026-11-13为未完周：仅显示最近已结束日2026-11-09的空心点，不进入收益率及相关计算。图表内周五是周标签，具体采样日可能为较早工作日；同步时刻仍未确认。

黄金收益率为简单收益率，利率变化为百分点差分。缺少本周或上周值即不计算该周变化；52周Pearson相关要求每一对变化均有效，缺口后须重新累积完整窗口。没有对水平值做相关回归，没有显著性检验，也没有经济归因。

事实比较表以实际保留的观测日期标签筛选固定日历窗口，首末点不一定为严格日历年末，因此不应称作年度投资收益。窗口为展示描述而指定，不代表自动识别的市场阶段。

## 验证与图形检查

```powershell
python -B -m unittest discover -s tests -v
git diff --check
```

测试覆盖共同采样、缺周不填充、未完周剔除、52对有效变化的要求，并用NumPy独立计算验证滚动相关。真实数据验证包括日期范围与唯一性、数值转换、非正或交叉黄金报价、年度条数、空周、星期分布以及FRED逐日核验；详细数值见质量文件。

图表输出为PNG、180dpi，使用可用中文字体，两条纵轴均正常向上。若环境没有中文字体，应先安装或配置中文字体，再检查最终图形；不得把缺字图当作交付结果。
