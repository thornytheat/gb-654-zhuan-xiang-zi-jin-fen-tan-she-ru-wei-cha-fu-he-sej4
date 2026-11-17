# 专项资金分摊与舍入尾差复核

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 预算采信：`/api/budgets/import`
- M2 资格校验：`/api/eligibility/evaluate`
- M3 权重测算：`/api/weights/calculate`
- M4 金额分摊：`/api/allocations/calculate`
- M5 尾差裁决：`/api/remainders/resolve`
- M6 批次复核：`/api/export/allocations.csv`
- M7 资金覆盖对账：`/api/coverage/ledgers`、`/api/coverage/revoke`

        ## 核心规则

        先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。

        M7 把项目获资承诺与来源占用分开记账。撤销指定来源时释放其原占用，已锁定来源保持不动，
        未锁定来源的占用整体重排做合规替补；替补不足保留为缺口而非削减承诺。
        封顶与到分尾差沿用 M4/M5 规则；并发争用同一来源在 BEGIN IMMEDIATE 事务内串行化，不超额、不重复占款。
        M7 只做覆盖对账与回读，不接支付、不扩展资格评分。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18213`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
