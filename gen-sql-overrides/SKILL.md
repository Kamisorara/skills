---
name: gen-sql-overrides
description: 为一个已有后端系统生成 sql_agent 的 skills/<db>.overrides.yaml 业务知识文件，把该系统接入问数能力。当用户要把某个系统/项目接入问数、要生成 overrides、要分析数据库表关系、要为问数准备 skill 配置、要把新库接到 sql_agent 时触发。流程是读目标系统代码（MyBatis/JPA/Django/SQLAlchemy/DDL）→ 抽表结构与关系 → 划分业务域（需用户确认）→ 补字段语义 → 生成 yaml → 自动验证。
---

# gen-sql-overrides

为一个已有后端系统生成 `skills/<db>.overrides.yaml`，把它接入 sql_agent 问数。
触发后按下面 7 步走：读代码 → 抽关系 → 分域（**第 4 步必须暂停问用户**）→ 补语义 → 生成 yaml → 自验证。

产物：`skills/<db_key>.overrides.yaml`（db_key 推断见步骤 6）。

---

## 核心工作流（7 步）

### 步骤 1：探索目标项目，判断技术栈

先看项目根的构建文件定语言，再找持久层线索文件定 ORM 类型：

| 线索文件 | 判定 |
|---|---|
| `pom.xml` / `build.gradle` | Java/Kotlin 项目 |
| `requirements.txt` / `pyproject.toml` | Python 项目 |
| `go.mod` | Go 项目 |

然后在源码里找持久层特征（决定步骤 2 读哪些文件）：

| 持久层技术 | 特征文件 |
|---|---|
| MyBatis | `*Mapper.xml` 或 `*Mapper.java`（含 SQL 注解 `@Select`） |
| JPA/Hibernate | `@Entity` 注解的类 |
| Django ORM | `models.py` 含 `class X(models.Model)` |
| SQLAlchemy | `.py` 里 `Column(ForeignKey(...))` |
| 纯 DDL/迁移 | `*.sql` / `db/migration/`（Flyway）/ `changelog.xml`（Liquibase） |
| Prisma | `schema.prisma` |

**探索时用 `find` / `glob` 扫描，不要只看一个目录**。把判断结果告诉用户（"这是 MyBatis 项目，我去读 *Mapper.xml"），让用户有机会纠正。

### 步骤 2：抽取所有表 + relationship

按步骤 1 判定的技术栈，用下面的速查表读对应文件。

**目标是产出两份清单**：
1. **全部表名**（与库内真实表名一致，不要改写）
2. **relationship 列表**：每条记录 `from=表.字段, to=表.字段, type, join, note`

各技术栈抽取规则见下方「技术栈速查」。

**关键**：表名以代码/DDL 里出现的为准，不要"美化"（代码里是 `t_order` 就写 `t_order`，别改成 `orders`）。表名拼错是最常见的接入失败，步骤 7 的验证会兜住。

### 步骤 3：聚类成候选业务域

用**双重启发式**把表聚成业务域：

1. **外键连通性**：外键链式关联的表聚一起（同一事务流程的表通常通过外键连接，如 orders→customers→customer_tiers）
2. **命名前缀**：相同前缀的表聚一起（如 `order_*`、`inv_*`、`sys_*`、`t_user_*`）

两者取**并集后合并**（一张表可能因外键归入 A 域、又因前缀归入 B 域——这时优先外键连通性，因为它反映真实的数据依赖）。

为每个候选域起名（用业务语义，如"销售域""库存域""用户域"，不要用"domain1"）。

### 步骤 4：⏸ 暂停，向用户确认域划分

**这一步必须停下来问用户，不要直接进入步骤 5。**

展示候选域划分，**附带聚类依据**，让用户能判断是否合理：

```
我根据代码分析，初步划分为 3 个业务域：

1. 销售域：orders, order_items, customers
   依据：orders.customer_id→customers（外键连通）+ order_ 前缀
2. 商品域：products, categories
   依据：products.category_id→categories（外键连通）
3. 系统域：sys_users, sys_roles, sys_permissions
   依据：sys_ 前缀

这样划分 OK 吗？要合并/拆分/调整哪些？有遗漏的表吗？
```

**为什么必须在域划分处确认**：域是业务判断，LLM 的聚类只是基于代码结构的建议。域划错了会影响整个 overrides 的组织结构（后续问数时 agent 按域加载知识），而 relationship 抽取相对客观（代码里 JOIN 是什么就是什么），错了也是小遗漏。所以把唯一的人工确认点放在 ROI 最高的域划分处，避免每步打断用户。

等用户确认（或调整）后再继续。

### 步骤 5：尽力抽取 field_notes，抽不到留 TODO

从代码里**尽力**抽字段级语义。能抽的：

| 语义来源 | 示例 | 抽取方式 |
|---|---|---|
| 枚举常量类 | `enum OrderStatus { PENDING, PAID, SHIPPED, CANCELLED }` | 读枚举类，把值列进 field_notes |
| 字典表 | `sys_dict` / `t_dict` 含 type+code+label | 读字典表内容（或提示用户核对） |
| 字段注释 | `// 状态: 0=有效 1=失效` 或 DDL `COMMENT '...'` | 从注释里抽枚举/单位 |
| 注解 | JPA `@Comment`、MyBatis `@Param` 说明 | 抽注解文本 |

**抽不到的，留 `# TODO: 需人工确认`**，绝不编造：
- 业务单位约定（"金额单位是分"——代码里只是个 int，看不出单位）
- 隐性软删除（`is_deleted` 的含义，0/1 还是 true/false）
- JSON 字段的内部结构（`ext_data` 里有什么键）

### 步骤 6：生成 skills/<db_key>.overrides.yaml

先确定 `<db_key>`（文件名，用于 `app/agent.py:_db_key_from_url` 定位）：

```
DATABASE_URL 为空            → demo
sqlite:///path/to/xx.db     → xx（去扩展名）
mysql://user:pwd@host/yy    → yy（取数据库名）
postgresql://.../zz         → zz
```

然后严格按下面的「overrides.yaml 格式模板」生成文件到 `skills/<db_key>.overrides.yaml`。

### 步骤 7：自动验证

运行验证脚本（确定性自检，把"接入后才发现表名对不上"前置到生成时）：

```bash
uv run python docs/skills/gen-sql-overrides/scripts/validate.py \
    skills/<db_key>.overrides.yaml "<DATABASE_URL>"
```

脚本检查 5 项：yaml 语法 / 必填键 / 表名在库中真实存在 / relationship 引用合法 / 可被 load_skills_registry 加载。

- **通过**（exit 0）：完成，告诉用户 yaml 已就绪，可跑 `uv run python main.py` 试问数。
- **失败**（exit 1）：读 stderr 的具体错误（会精确到哪个表名拼错），修正 yaml 后重跑，直到通过。

---

## 技术栈速查（步骤 2 用）

每种技术栈找什么文件、抽什么：

| 技术栈 | 找什么 | relationship 怎么抽 |
|---|---|---|
| **MyBatis** | `*Mapper.xml`（`<resultMap>`, `<select>`, `<join>`）/ `*Mapper.java`（`@Select` 注解） | 从 SQL 的 `JOIN ... ON a.x = b.y` 抽：from=a.x, to=b.y |
| **JPA/Hibernate** | `@Entity` 类 | `@ManyToOne`/`@OneToMany` → type；`@JoinColumn(name="x_id")` → from=本表.x_id, to=目标表.id |
| **Django ORM** | `models.py` | `ForeignKey(OtherModel, ...)` → from=本表.othermodel_id, type=many_to_one；`ManyToManyField` → many_to_many |
| **SQLAlchemy** | `*.py` model 定义 | `Column(ForeignKey('othertable.id'))` → from/to；`relationship()` 标注类型 |
| **DDL/迁移**（金标准） | `*.sql` / Flyway `V*.sql` / Liquibase changelog | `FOREIGN KEY (x_id) REFERENCES othertable(id)` → from/to，最可靠 |
| **Prisma** | `schema.prisma` | `relation([fields: [xId], references: [id]])` → from/to |

**优先级**：如果项目同时有 DDL 和 ORM（如 JPA 项目带 Flyway 迁移），**以 DDL 的 FOREIGN KEY 为准**——它是数据库层的硬约束，最可靠；ORM 注解有时和实际 schema 有出入。

---

## overrides.yaml 格式模板（步骤 6 用）

五层结构：①结构层（inspector 自动读，**不写进 yaml**）+ ②field_notes + ③relationships + ④business_terms + ⑤example_queries。照填：

```yaml
# Skills 业务知识覆盖文件
# 信息分层（详见 PLAN_v2.md §3.5）：
#   ① 结构层：表/列/外键/样例 —— inspector 自动读，不写这里
#   ② field_notes：字段标注（枚举/单位）—— 尽力抽，抽不到留 TODO
#   ③ relationships：表关系（join 路径）—— 真实库无外键时全靠这层
#   ④ business_terms：业务口径 —— 鼓励可执行 SQL 谓词
#   ⑤ example_queries：范例 SQL（few-shot）—— 抽不到留 TODO

domains:
  - name: 销售域
    description: <一句话描述本域业务>
    tables: [orders, order_items, customers]   # 必须与库内真实表名一致
    field_notes:                               # 尽力抽，抽不到的键省略或留 TODO
      "orders.status": "枚举: pending/paid/shipped/cancelled (来源: OrderStatus.java)"
      "orders.amount": "# TODO: 需人工确认单位（分还是元）"
    relationships:
      - from: orders.customer_id
        to: customers.customer_id
        type: many_to_one                      # many_to_one / one_to_many / many_to_many
        join: "orders.customer_id = customers.customer_id"
        note: 订单归属客户
    business_terms:
      - term: <业务术语>
        definition: "<可执行 SQL 谓词优先，如 status='completed' AND ...；自然语言亦可>"
    example_queries:
      - |-                                     # 多行 SQL 用 YAML 块标量 |-，避免 \n 转义
        -- <查询说明>
        SELECT ...
```

**写法要点**：
- `example_queries` 和多行字符串**必须用 `|-` 块标量**，不要用 `"\n"` 转义（容易出错）
- `field_notes` 的键用引号包住（`"orders.status"`），因为含点号
- relationship 的 `from`/`to` 格式严格是 `表.字段`

---

## 不编造原则（质量红线）

这是生成本文件最重要的原则，单独强调：

**绝不为 field_notes / business_terms / example_queries 编造内容。**

- 枚举值：只在代码里找到枚举类/字典表/注释时才填；没找到 → `# TODO: 需人工确认枚举值`
- 单位：代码里只是个数字字段看不出单位 → `# TODO: 需人工确认单位`
- 范例 SQL：不要编造"看起来合理"的查询 → `# TODO: 需人工补充业务典型查询`
- 业务口径：不要猜"活跃客户"的定义 → `# TODO: 需人工定义`

**为什么**：编造的语义会被 agent 当真，导致问数结果系统性错误（按错误的枚举值过滤、按错误的口径计算）。留 TODO 是诚实的——它明确告诉用户"这里需要你来补"，而编造会制造"看起来完整实则错误"的配置，更危险。

宁可生成一个"结构完整、语义半空（带 TODO）"的 yaml，也不要生成一个"看似完整实则编造"的 yaml。前者用户补几处就能用，后者会让用户在问数出错时很难定位问题根源。

---

## 完成后

1. 告诉用户生成的文件路径和验证结果（步骤 7 的输出）
2. 列出所有 `# TODO` 项，提示用户这些需要人工补充
3. 建议用户跑一次 `uv run python main.py -q "<该域的一个典型问题>"` 做端到端验证
