"""出品確定済みセラーの実績を、明示されたレポートの起点の優先順へ返す。"""
from sqlalchemy import text
from seller_performance.attribution import AttributionService


def refresh_attribution(factory, market, previous):
    with factory.get_session(market) as conn, conn.begin():
        version = conn.execute(text(
            "SELECT max(executed_at) FROM import_run_history WHERE marketplace=:m "
            "AND status='COMPLETED' AND import_type IN ('WEEKLY_LISTING','TOOL_OUTPUT')"
        ), {"m": market}).scalar_one()
    if version == previous:
        return version, 0
    with factory.get_session(market) as conn:
        count = AttributionService().attribute(conn, market)
    return version, count


def origin_priorities(factory, business_market, page_market, import_id):
    # 業務方向の実績と取得先の出典は、それぞれのRLS区画で読む。
    with factory.get_session(business_market) as conn, conn.begin():
        listed = dict(conn.execute(text(
            "SELECT seller_id,count(*) FROM seller_attribution "
            "WHERE marketplace=:m AND listed_at IS NOT NULL GROUP BY seller_id"
        ), {"m": business_market}).all())
    with factory.get_session(page_market) as conn, conn.begin():
        paths = conn.execute(text(
            "SELECT DISTINCT p.origin_asin,s.key_primary FROM acquisition_path p "
            "JOIN acquisition_source s ON s.source_id=p.source_id AND s.marketplace=p.marketplace "
            "WHERE p.marketplace=:page AND p.origin_marketplace=:business "
            "AND p.origin_import_id=:i AND p.direction=:direction AND s.source_type='SELLER'"
        ), {"page": page_market, "business": business_market, "i": import_id,
             "direction": "EXPORT" if business_market == "US" else "IMPORT"}).all()
    scores = {}
    for asin, seller in paths:
        scores[asin] = scores.get(asin, 0) + int(listed.get(seller, 0))
    return scores
