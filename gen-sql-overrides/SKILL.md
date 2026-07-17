---
name: gen-sql-overrides
description: 为一个已有 Spring Boot 后端系统生成 sql_agent 的 skills/<db>.overrides.yaml 业务知识文件，把该系统接入问数能力。当用户要把某个 Spring Boot 系统/项目接入问数、要生成 overrides、要分析数据库表关系、要为问数准备 skill 配置、要把新库接到 sql_agent 时触发。流程是读目标 Spring Boot 项目代码（MyBatis-Plus / Spring Data JPA 实体、Mapper、application.yml、Flyway 迁移）→ 抽表结构与关系 → 划分业务域（需用户确认）→ 补字段语义 → 生成 yaml → 自动验证。
---

# gen-sql-overrides

为一个已有 **Spring Boot** 后端系统生成 `skills/<db_key>.overrides.yaml`，把它接入 sql_agent 问数。
本 skill 专精 Spring Boot 两大持久层：**MyBatis-Plus（主）** 和 **Spring Data JPA（辅）**。非 Java 项目请勿使用。

触发后按下面 7 步走：读代码 → 抽关系 → 分域（**第 4 步必须暂停问用户**）→ 补语义 → 生成 yaml → 自验证。

产物：`skills/<db_key>.overrides.yaml`（db_key 推断见步骤 6）。

---

## 核心工作流（7 步）

### 步骤 1：确认是 Spring Boot 项目，判定持久层主路线

**先确认是 Java + Spring Boot**：项目根有 `pom.xml` 或 `build.gradle`，且依赖里含 `spring-boot-starter`（或 parent 是 `spring-boot-starter-parent`）。不是 Spring Boot 项目就停，告诉用户本 skill 不适用。

**再判定持久层主路线**——读 `src/main/resources/application.yml`（或 `application.properties` / `application-*.yml`）：

| 配置段出现 | 主路线 |
|---|---|
| `mybatis-plus:` | **MyBatis-Plus**（Spring Boot 中文社区主流） |
| `spring.jpa:` / `spring.hibernate:` / `hibernate.` | **Spring Data JPA** |
| 两者都有 | 以实体注解特征为准（见下），通常 MyBatis-Plus 是主 |

**用实体类注解特征交叉验证**（扫 `src/main/java/**/entity/` 或 `**/domain/` 或 `**/po/`）：

| 特征 | 判定 |
|---|---|
| 类上有 `@TableName("xxx")`、字段有 `@TableField`/`@TableId`、Mapper `extends BaseMapper<T>` | **MyBatis-Plus** |
| 类上有 `@Entity` + `@Table(name="xxx")`、字段有 `@Column`、Repository `extends JpaRepository<T, ID>` | **Spring Data JPA** |

```java
// MyBatis-Plus 实体特征
@TableName("fwq_project")
public class FwqProject extends SimpleBaseEntity {
    @TableId(type = IdType.ASSIGN_ID)
    private Long id;
    @TableField("project_name")
    private String name;
    @TableField(exist = false)      // ← 非持久化字段，绝不当列抽
    private String userName;
}

// Spring Data JPA 实体特征
@Entity
@Table(name = "fwq_project")
public class FwqProject {
    @Id @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "id")
    private Long id;
    @Column(name = "project_name")
    private String name;
}
```

把判定结果告诉用户（"这是 MyBatis-Plus 项目，主路线读 `@TableName` 实体 + `*Mapper.xml`"），让用户有机会纠正。判定结果决定步骤 2 读哪些文件、抽什么。

---

### 步骤 2：抽取所有表 + relationship（按主路线用对应速查）

**目标是产出两份清单**：
1. **全部表名**（与库内真实表名一致，不要改写、不要用类名）
2. **relationship 列表**：每条记录 `from=表.字段, to=表.字段, type, join, note`

表名以代码/DDL 里出现的为准，不要"美化"（代码里是 `t_order` 就写 `t_order`，别改成 `orders`）。表名拼错是最常见的接入失败，步骤 7 的验证会兜住。

按步骤 1 判定的主路线，用下方「MyBatis-Plus 抽取速查」或「Spring Data JPA 抽取速查」。

#### 2A. MyBatis-Plus 抽取速查

| 信号 | 找什么 | 抽什么 → 落到哪层 |
|---|---|---|
| **表名权威来源** | 实体类 `@TableName("fwq_project")`；没注解则按驼峰转下划线（`FwqProject`→`fwq_project`） | tables |
| **`@TableField(exist = false)`** ⚠️ | 实体字段上该注解 | **跳过此字段，绝不当列抽**（否则凭空造出幽灵列，问数时 SQL 报错） |
| **`@TableLogic` 软删除** ⚠️⚠️ | 实体字段 `@TableLogic`，或 `application.yml: mybatis-plus.global-config.db-config.logic-delete-field/value/not-delete-value` | 抽出软删字段名 + 0/1 语义 → field_notes（所有继承表统一）+ 头部全局约定块 |
| **`@TableLogic` 在但 DDL 无此列** ⚠️⚠️⚠️ | 实体继承基类带 `@TableLogic`，但真实库 DDL 无 `del_flag` 列 | **单独标红警告**：该表 MyBatis-Plus 自动查询会注入 `AND del_flag=0` 导致 SQL 报错，只能用自定义 Mapper → 头部全局约定块 |
| **`@DS("xxx")` 多数据源** ⚠️⚠️ | Mapper 接口/Service 类上的 `@DS("zentao")`（com.baomidou 动态数据源） | **整张表排除出 overrides**（在另一库，查了必报错），记录到头部全局约定的「排除清单」 |
| **`@TableId(type=...)`** | 主键字段 | 抽主键策略（AUTO/ASSIGN_ID/INPUT）→ field_notes |
| **基类公共字段** | `class XxxEntity extends SimpleBaseEntity` / `BaseEntity` | 读基类一次，抽出公共字段（id/create_time/update_time/del_flag/version）应用到所有继承表 → field_notes |
| **空 `BaseMapper<T>`**（无 JOIN 可抽）⚠️ | Mapper 接口只 `extends BaseMapper<FwqProject>`，无 `@Select`/无对应 `*Mapper.xml` | **明确：此表无 SQL JOIN 可抽**。关系改去 Service 层 `LambdaQueryWrapper` / `eq(...)` / `in(...)` 里找软关联 |
| **`@Select`/`@Update` 注解里的 JOIN** | `*Mapper.java` 方法上的 SQL 注解 | 抽 `JOIN ... ON a.x = b.y` → relationships |
| **`*Mapper.xml` 的 JOIN** | `src/main/resources/mapper/**/*Mapper.xml` 的 `<select>` / `<resultMap>` | 抽 `<join>` / `LEFT JOIN ... ON` → relationships |
| **命名策略** | `application.yml: mybatis-plus.configuration.map-underscore-to-camel-case`（默认 true） | 字段 `userName` ↔ 列 `user_name`，无显式 `@TableField` 时按此映射 |
| **Flyway 金标准** ⭐ | `src/main/resources/db/migration/V*.sql` 的 `FOREIGN KEY (x_id) REFERENCES othertable(id)` | **同时有 Flyway 和实体时，以 DDL 的 FOREIGN KEY 为准**（最可靠）→ relationships |

**关于空 `BaseMapper` 的特别说明**：这是 MyBatis-Plus 项目和裸 MyBatis 最大的行为差异。很多项目（如 sos_portal 的 `fwq_*` 域）所有 Mapper 都是空 `BaseMapper`，没有任何 SQL JOIN——关系全部在 Service 层用 `LambdaQueryWrapper` 拼装。这时不要去 `*Mapper.xml` 找 JOIN（会大面积扑空），要去 Service 层找：

```java
// Service 层的软关联，抽 relationship 时认这种写法
List<FwqServer> servers = serverMapper.selectList(
    Wrappers.<FwqServer>lambdaQuery()
        .eq(FwqServer::getProjectId, project.getId())   // ← project_id 软关联
);
// → relationship: from=fwq_server.project_id, to=fwq_project.id, type=many_to_one
```

#### 2B. Spring Data JPA 抽取速查

| 信号 | 找什么 | 抽什么 → 落到哪层 |
|---|---|---|
| **表名权威来源** | `@Entity` + `@Table(name="fwq_project")`；无 `@Table` 则按 `CamelCaseToUnderscoresNamingStrategy`（Spring Boot 默认）驼峰转蛇形 | tables |
| **列名** | `@Column(name="project_name")`；无注解则按命名策略（`projectName`→`project_name`） | （列名准确性） |
| **关系注解** ⭐ | `@ManyToOne`/`@OneToMany`/`@OneToOne`/`@ManyToMany` + `@JoinColumn(name="customer_id")` | 抽 from=本表.customer_id, to=目标表.id, type → relationships |
| **反向端 `mappedBy`** | `@OneToMany(mappedBy="order")` 在被引用方的类上 | **注意别重复抽**：mappedBy 端是反向，只抽 `@JoinColumn` 那一侧 |
| **`@Comment`** | Hibernate 的 `@Column(columnDefinition="... COMMENT '...'")` 或 `@Comment("...")` | 抽注释 → field_notes |
| **Flyway 金标准** ⭐ | `src/main/resources/db/migration/V*.sql` 的 `FOREIGN KEY ... REFERENCES` | **以 DDL 的 FOREIGN KEY 为准**（JPA 注解有时和实际 schema 有出入）→ relationships |

关系 type 映射：`@ManyToOne`→many_to_one，`@OneToMany`→one_to_many，`@OneToOne`→one_to_one，`@ManyToMany`→many_to_many。

---

### 步骤 3：聚类成候选业务域（三重启发式）

把表聚成业务域，用**三个信号**：

1. **Java 包结构**（Spring Boot 最强信号）：同一业务包下的表聚一起。`com.xxx.order.entity`/`com.xxx.order.mapper` 下的表 → 订单域；`com.xxx.inventory.*` → 库存域。Spring Boot 项目常按业务包组织代码，包边界天然是业务域边界。
2. **外键/软关联连通性**：外键链式关联的表聚一起（orders→customers→customer_tiers）。空 BaseMapper 的项目里，看 Service 层 `LambdaQueryWrapper` 的跨表查询连出来的表。
3. **命名前缀**：相同前缀聚一起（`order_*`、`inv_*`、`sys_*`、`t_user_*`）。

**优先级**：包结构 ≈ 外键连通性 > 命名前缀。三者取**并集后合并**——一张表可能因包结构归入 A 域、又因前缀归入 B 域，这时优先包结构和外键连通性（它们反映真实业务/数据依赖）。

为每个候选域起名（用业务语义，如"销售域""库存域""用户域"，不要用"domain1"）。

---

### 步骤 4：⏸ 暂停，向用户确认域划分

**这一步必须停下来问用户，不要直接进入步骤 5。**

展示候选域划分，**附带聚类依据**，让用户能判断是否合理：

```
我根据 Spring Boot 代码分析，初步划分为 3 个业务域：

1. 销售域：orders, order_items, customers
   依据：com.xxx.sale 包下 + orders.customer_id→customers 软关联 + order_ 前缀
2. 商品域：products, categories
   依据：com.xxx.product 包下 + products.category_id→categories 软关联
3. 系统域：sys_users, sys_roles, sys_permissions
   依据：com.xxx.system 包下 + sys_ 前缀

这样划分 OK 吗？要合并/拆分/调整哪些？有遗漏的表吗？
```

**为什么必须在域划分处确认**：域是业务判断，LLM 的聚类只是基于代码结构的建议。域划错了会影响整个 overrides 的组织结构（后续问数时 agent 按域加载知识），而 relationship 抽取相对客观（代码里 JOIN 是什么就是什么），错了也是小遗漏。所以把唯一的人工确认点放在 ROI 最高的域划分处，避免每步打断用户。

等用户确认（或调整）后再继续。

---

### 步骤 5：尽力抽取 field_notes，抽不到留 TODO

从代码里**尽力**抽字段级语义。Spring Boot 项目能抽的：

| 语义来源 | 示例 | 抽取方式 |
|---|---|---|
| **Java 枚举类** | `public enum OrderStatus { PENDING, PAID, SHIPPED, CANCELLED }`（常带 `@JsonValue`/`@EnumValue`） | 读枚举类，把值列进 field_notes |
| **MyBatis-Plus `@TableField` 注解** | `@TableField("状态: 0=有效 1=失效")` 或字段 Javadoc | 抽注解/Javadoc 文本 |
| **JPA `@Column` / `@Comment`** | `@Comment("金额，单位分")` | 抽注解文本 |
| **字典表** | `sys_dict` 含 type+code+label | 读字典表内容（或提示用户核对） |
| **字段注释** | `// 状态: 0=有效 1=失效` 或 DDL `COMMENT '...'` | 从注释里抽枚举/单位 |

**抽不到的，留 `# TODO: 需人工确认`**，绝不编造：
- 业务单位约定（"金额单位是分"——代码里只是个 int，看不出单位）
- 隐性软删除（`is_deleted` 的含义，0/1 还是 true/false）
- JSON 字段的内部结构（`ext_data` 里有什么键）

---

### 步骤 6：生成 skills/<db_key>.overrides.yaml

先确定 `<db_key>`（文件名，用于 `app/agent.py:_db_key_from_url` 定位）：

```
DATABASE_URL 为空            → demo
sqlite:///path/to/xx.db     → xx（去扩展名）
mysql://user:pwd@host/yy    → yy（取数据库名）
postgresql://.../zz         → zz
```

然后严格按下面的「overrides.yaml 格式模板」生成文件到 `skills/<db_key>.overrides.yaml`。

---

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

## overrides.yaml 格式模板（步骤 6 用）

**头部全局约定块 + 五层结构**：①结构层（inspector 自动读，**不写进 yaml**）+ ②field_notes + ③relationships + ④business_terms + ⑤example_queries。

Spring Boot 项目的全局约定（软删除、跨库排除、公共字段）写在**文件顶部的注释块**里——这块会随 `load_skill` 一起被 agent 读到，是承载"影响所有表的运行时坑"的最佳位置。

```yaml
# =============================================================================
# Skills 业务知识覆盖文件
# 目标数据库: <db_key> (<数据库类型与版本>)
# 生成依据: <列出读过的源：实体类 / *Mapper.xml / application.yml / Flyway V*.sql>
# =============================================================================
# ⚠️ 重要全局约定（基于 Spring Boot 代码核对，问数时必须遵守）：
#
# 1. 软删除字段：<del_flag>（int，0=未删除,1=已删除）。
#    来源：SimpleBaseEntity 的 @TableLogic + application.yml: mybatis-plus.global-config.
#    db-config.logic-delete-field=del_flag / logic-delete-value=1 / logic-not-delete-value=0。
#    MyBatis-Plus 自动查询会注入 AND del_flag = 0。
#
# 2. ⚠️ 有 @TableLogic 但 DDL 无 del_flag 列的表（运行时自动查询必报错，只能自定义 Mapper）：
#    - <user_gitlab_binding>（继承 SimpleBaseEntity 但 DDL 实际无 del_flag 列）
#
# 3. @DS 跨库表已从本文件排除（sql_agent 一次只连一个库）：
#    - @DS("<zentao>")：<zt_project, zt_task, ...>（zentao 库）
#    - @DS("<confluence>")：<content, bodycontent, spaces>（confluence 库）
#
# 4. 公共字段（继承 SimpleBaseEntity 的表都有）：id / create_time / update_time / del_flag
# =============================================================================

domains:
  - name: 销售域
    description: <一句话描述本域业务>
    tables: [orders, order_items, customers]   # 必须与库内真实表名一致
    field_notes:                               # 尽力抽，抽不到的键省略或留 TODO
      "orders.status": "枚举: pending/paid/shipped/cancelled (来源: OrderStatus.java)"
      "orders.amount": "# TODO: 需人工确认单位（分还是元）"
      "orders.del_flag": "软删除: 0=未删除,1=已删除（@TableLogic 自动注入）"
    relationships:
      - from: orders.customer_id
        to: customers.customer_id
        type: many_to_one                      # many_to_one / one_to_many / one_to_one / many_to_many
        join: "orders.customer_id = customers.customer_id"
        note: 订单归属客户（空 BaseMapper，软关联来自 OrderService LambdaQueryWrapper）
    business_terms:
      - term: <业务术语>
        definition: "<可执行 SQL 谓词优先，如 status='completed' AND ...；自然语言亦可>"
    example_queries:
      - |-                                     # 多行 SQL 用 YAML 块标量 |-，避免 \n 转义
        -- <查询说明>
        SELECT ...

# （可选）跨域关系：放在顶层，会被注入到每个域。无跨域关系则省略此键。
# cross_domain_relationships:
#   - from: orders.product_id
#     to: products.id
#     type: many_to_one
#     join: "orders.product_id = products.id"
#     note: 订单引用商品（跨销售域-商品域）
```

**写法要点**：
- 头部全局约定块用 `#` 注释，**不进 `domains` 结构**——现有 loader 只读 `domains` 和 `cross_domain_relationships` 两个顶层键，注释被忽略，但 agent `load_skill` 时会连同注释一起读到
- `example_queries` 和多行字符串**必须用 `|-` 块标量**，不要用 `"\n"` 转义（容易出错）
- `field_notes` 的键用引号包住（`"orders.status"`），因为含点号
- relationship 的 `from`/`to` 格式严格是 `表.字段`
- `note` 字段记下关系的来源（`@JoinColumn` / `@Select` JOIN / Service 层 LambdaQueryWrapper），方便后续校对

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
3. 列出头部全局约定里的"⚠️ 警告项"（如 `@TableLogic` 在但 DDL 无此列的表），提示用户这些是运行时风险点
4. 建议用户跑一次 `uv run python main.py -q "<该域的一个典型问题>"` 做端到端验证
