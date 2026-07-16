"""验证生成的 overrides.yaml 能被 sql_agent 正确加载。

gen-sql-overrides skill 的步骤 7 调用本脚本，对刚生成的 yaml 做确定性自检：
把"接入后才发现表名对不上 / 格式有错"的问题前置到生成时。

用法:
    python docs/skills/gen-sql-overrides/scripts/validate.py <yaml路径> <DATABASE_URL>

检查项（5 项，对应 spec §八）:
    1. yaml 语法正确（yaml.safe_load 不报错）
    2. 必填键齐全（domains[].name / domains[].tables 至少存在）
    3. 所有 tables 在目标库真实存在（对比 inspector.get_table_names()）
    4. relationship 的 from/to 引用的表都在该域 tables 列表内
    5. 能被 load_skills_registry 成功加载（端到端，复用 app.skills）

退出码: 0 = 全通过; 1 = 有问题（stderr 打印具体错误）
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from sqlalchemy import create_engine, inspect


def _fail(msg: str) -> None:
    """打印错误到 stderr 并以退出码 1 结束。"""
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def _check_yaml_syntax(yaml_path: Path) -> dict:
    """检查项 1：yaml 语法。返回解析后的 dict。"""
    try:
        with yaml_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        _fail(f"yaml 语法错误：{e}")
    if not isinstance(data, dict):
        _fail(f"yaml 顶层应是映射，实际是 {type(data).__name__}")
    return data


def _check_required_keys(data: dict) -> list[dict]:
    """检查项 2：每个 domain 必须有 name 和 tables。返回 domains 列表。"""
    domains = data.get("domains")
    if not isinstance(domains, list) or not domains:
        _fail("缺少 domains 列表或为空（顶层需有 domains: [...]）")
    for i, dom in enumerate(domains):
        if not isinstance(dom, dict):
            _fail(f"domains[{i}] 应是映射，实际是 {type(dom).__name__}")
        if not dom.get("name"):
            _fail(f"domains[{i}] 缺少必填键 name")
        tables = dom.get("tables")
        if not isinstance(tables, list) or not tables:
            _fail(f"domains[{i}] ({dom.get('name')}) 缺少必填键 tables 或为空")
    return domains


def _check_tables_exist(domains: list[dict], valid_tables: set[str]) -> None:
    """检查项 3：所有 tables 在目标库真实存在。"""
    for dom in domains:
        dom_name = dom["name"]
        for t in dom["tables"]:
            if not isinstance(t, str):
                _fail(f"域 {dom_name} 的 tables 含非字符串项：{t!r}")
            if t not in valid_tables:
                _fail(
                    f"域 {dom_name} 引用了不存在的表 {t!r}。"
                    f"库内实际表：{sorted(valid_tables)}"
                )


def _check_relationships(domains: list[dict]) -> None:
    """检查项 4：relationship 的 from/to 引用的表都在该域 tables 内。

    from/to 形如 'orders.customer_id'，取点号前的表名校验。
    跨域引用（from 的表不在本域 tables 但在别的域）仅告警不失败，
    因为跨域关系是合法设计（见 PLAN_v2 §3.5 后续可选增强）。
    """
    # 收集所有域的表，用于跨域告警
    all_tables: dict[str, str] = {}  # table → domain_name
    for dom in domains:
        for t in dom["tables"]:
            all_tables[t] = dom["name"]

    for dom in domains:
        dom_name = dom["name"]
        dom_tables = set(dom["tables"])
        for rel in dom.get("relationships", []) or []:
            for side in ("from", "to"):
                val = rel.get(side, "")
                if "." not in val:
                    _fail(
                        f"域 {dom_name} 的 relationship.{side}={val!r} "
                        "格式应为 '表.字段'"
                    )
                tbl = val.split(".", 1)[0]
                if tbl not in dom_tables:
                    if tbl in all_tables:
                        # 跨域引用：合法，仅告警
                        print(
                            f"⚠ 域 {dom_name} 的 relationship.{side}={val!r} "
                            f"引用了 {all_tables[tbl]} 域的表 {tbl}（跨域关系，已放行）",
                            file=sys.stderr,
                        )
                    else:
                        _fail(
                            f"域 {dom_name} 的 relationship.{side}={val!r} "
                            f"引用了不存在的表 {tbl!r}"
                        )


def _check_loadable(yaml_path: Path, engine) -> list:
    """检查项 5：能被 app.skills.load_skills_registry 成功加载（端到端）。"""
    # 延迟导入：app.skills 依赖项目的 sqlalchemy engine，确保在项目根运行
    # 路径深度：validate.py → scripts(0) → gen-sql-overrides(1) → skills(2) → docs(3) → 项目根(4)
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    try:
        from app.skills import load_skills_registry  # type: ignore
    except ImportError as e:
        _fail(f"无法导入 app.skills.load_skills_registry：{e}（请在项目根目录运行）")
    try:
        registry = load_skills_registry(engine, yaml_path)
    except Exception as e:  # noqa: BLE001
        _fail(f"load_skills_registry 加载失败：{e}")
    return registry


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        _fail("用法：validate.py <yaml路径> <DATABASE_URL>")

    yaml_path = Path(sys.argv[1]).resolve()
    db_url = sys.argv[2]

    if not yaml_path.exists():
        _fail(f"yaml 文件不存在：{yaml_path}")

    print(f"验证 {yaml_path.name} ...")

    # 检查 1: yaml 语法
    data = _check_yaml_syntax(yaml_path)
    print("  ✓ yaml 语法正确")

    # 检查 2: 必填键
    domains = _check_required_keys(data)
    print(f"  ✓ 必填键齐全（{len(domains)} 个业务域）")

    # 连库：表名存在性校验需要
    try:
        engine = create_engine(db_url)
        valid_tables = set(inspect(engine).get_table_names())
    except Exception as e:  # noqa: BLE001
        _fail(f"无法连接数据库（{db_url}）：{e}")

    # 检查 3: 表名真实存在
    _check_tables_exist(domains, valid_tables)
    print(f"  ✓ 所有表名在库中真实存在（{len(valid_tables)} 张表）")

    # 检查 4: relationship 引用合法
    _check_relationships(domains)
    print("  ✓ relationship 引用合法")

    # 检查 5: 可被 load_skills_registry 加载
    registry = _check_loadable(yaml_path, engine)
    total_tables = sum(len(s.tables) for s in registry)
    total_rels = sum(len(s.relationships) for s in registry)
    print(
        f"  ✓ load_skills_registry 加载成功"
        f"（{len(registry)} 域 / {total_tables} 表 / {total_rels} 关系）"
    )

    print(f"\n✅ 全部通过：{yaml_path}")


if __name__ == "__main__":
    main()
