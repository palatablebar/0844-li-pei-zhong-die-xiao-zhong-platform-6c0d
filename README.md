# 理赔重叠消重台

医保理赔审核员处理重叠消重与支付方顺序的本地工作台。样例、规则和复核记录都在项目内，不依赖外部服务。

## 启动

```bash
docker compose up --build
```

打开 `http://127.0.0.1:${PORT:-18520}`。默认端口见 `docker-compose.yml`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

## 操作

1. 在左侧选阶段，查看采信、整理、计算、判定和对账结果。
2. 选择 `dirty` 样例观察坏行和隔离原因。
3. 换 `clean` 样例执行批次，填写复核意见后签发。
4. 下载证据 JSON，核对来源摘要、指标和签发版本。
5. 在首页“银行到账对账”面板（或打开 `/bank`）载入歧义/晚到场景，核对已结应付与挂账金额，并重放同一流水验证幂等。

业务规则见 `docs/BUSINESS-RULES.md`，字段定义见 `docs/DATA-DICTIONARY.md`，银行对账接口与匹配规则见 `docs/BANK-RECONCILIATION.md`。
