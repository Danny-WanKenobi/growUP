"""Sequential source traversal over offline fixtures; no provider/database calls."""
import json
from dataclasses import replace
from uuid import uuid4
import pytest
from tests.test_discovery_runner import _FakeConn,_db,_config,_cells,_no_sabbath,_Pass
from app.discovery_runner import run_worker
from app.agents.gabriel.sources.base import SourceError


def test_finish_first_source_before_second_and_handoff_between_pages():
    db=_db(); a,b=_cells(); a=replace(a,source_rank=0); b=replace(b,source_rank=1)
    log=[]; passes={a.unit_key:0,b.unit_key:0}
    pid=str(uuid4())
    def discover(cell,run_id="",limit=0):
        passes[cell.unit_key]+=1
        n=passes[cell.unit_key]
        log.append(("search",cell.unit_key,n))
        result=_Pass(1,(pid,)) if cell is a and n==1 else _Pass(0)
        # The middle page is all duplicates/filtered rows, not end-of-source.
        result.source_exhausted = cell is b or n==3
        result.next_cursor = None if result.source_exhausted else str(n)
        return result
    def evaluate(scope,property_id):
        log.append(("handoff",property_id))
        return {"outcome":"evaluated","decision":"needs_evidence","qualified":1}
    report=run_worker(lambda:_FakeConn(db),_config(cells=[b,a],max_passes=8),
        discovery_fn=discover,evaluate_fn=evaluate,counts_fn=lambda:pytest.fail("quota store must not be read"),
        sleep_fn=lambda _:None,now_fn=_no_sabbath)
    assert [r[1] for r in log if r[0]=="search"] == [a.unit_key]*3+[b.unit_key]
    assert log[1] == ("handoff",pid)
    assert report["reason"] == "all approved source scopes exhausted"
    assert report["remaining_quota"] is None


def test_resume_keeps_page_cursor_in_current_source():
    db=_db(); seen=[]
    def discover(cell,run_id="",limit=0):
        state=db["runs"][-1]["checkpoint"]["cell_state"].get(cell.unit_key,{})
        seen.append((cell.unit_key,state.get("continuation")))
        result=_Pass(0); result.source_exhausted=False;result.next_cursor="page-two"
        return result
    kwargs=dict(discovery_fn=discover,counts_fn=lambda:{},sleep_fn=lambda _:None,now_fn=_no_sabbath)
    run_worker(lambda:_FakeConn(db),_config(max_passes=1),**kwargs)
    run_worker(lambda:_FakeConn(db),_config(max_passes=1,resume=True),**kwargs)
    assert seen == [(_cells()[0].unit_key,None),(_cells()[0].unit_key,"page-two")]


def test_unknown_pagination_does_not_claim_source_complete():
    db=_db()
    def discover(*a,**k):
        result=_Pass(0);result.source_exhausted=None;result.next_cursor=None;return result
    report=run_worker(lambda:_FakeConn(db),_config(cells=[_cells()[0]]),discovery_fn=discover,
        counts_fn=lambda:{},sleep_fn=lambda _:None,now_fn=_no_sabbath)
    assert "coverage incomplete" in report["reason"]
    assert report["exhausted_units"] == []


def test_dcad_pages_do_not_repeat_and_snapshot_change_refuses(tmp_path,monkeypatch):
    from tests.test_dcad_bulk import _stage,BBOX
    from app.agents.gabriel.sources.dcad_bulk import DcadBulkSource
    _stage(tmp_path);monkeypatch.setenv("DCAD_BULK_DATA_DIR",str(tmp_path))
    source=DcadBulkSource(); cats=["high_rise","commercial_property","warehouse_industrial","other"]
    first=source.search_page(BBOX,cats,1,5)
    second=source.search_page(BBOX,cats,1,5,first.next_cursor)
    assert first.items[0].external_id != second.items[0].external_id
    assert not first.exhausted and second.exhausted
    path=tmp_path/"dcad_accounts.csv";path.write_text(path.read_text()+"\n")
    with pytest.raises(SourceError,match="snapshot changed"):
        source.search_page(BBOX,cats,1,5,first.next_cursor)


def test_socrata_cursor_uses_raw_rows_not_filtered_or_created_count():
    from app.agents.gabriel.sources.generic_open_data import GenericSocrataSource
    from tests.test_us_discovery import _binding,BBOX
    calls=[]
    def fetch(url,params,timeout):
        calls.append(dict(params))
        # First page outside scope; still continue to the next raw page.
        rows=([{ "account":"A1","latitude":0,"longitude":0,"land_use":"WAREHOUSE"}]
              if params["$offset"]=="0" else [])
        return json.dumps(rows).encode()
    source=GenericSocrataSource(fetch=fetch,binding=_binding())
    first=source.search_page(BBOX,["warehouse_industrial"],1,5)
    assert first.items==[] and not first.exhausted
    second=source.search_page(BBOX,["warehouse_industrial"],1,5,first.next_cursor)
    assert second.exhausted
    assert [c["$offset"] for c in calls]==["0","1"]
    assert all(c["$order"]==":id" for c in calls)


def test_arcgis_requires_stable_identity_before_request():
    from app.agents.gabriel.sources.generic_open_data import GenericArcGISSource
    from tests.test_us_discovery import _binding,BBOX
    binding=_binding(adapter="generic_arcgis",platform="arcgis")
    binding["fields"]=["address"]
    source=GenericArcGISSource(fetch=lambda *a:pytest.fail("must refuse before transmitting"),binding=binding)
    with pytest.raises(SourceError,match="stable object-id"):
        source.search_page(BBOX,["warehouse_industrial"],1,5)


def test_provider_ignoring_offset_is_blocked_not_called_exhausted():
    from app.agents.gabriel.sources.generic_open_data import GenericSocrataSource
    from tests.test_us_discovery import _binding,BBOX
    body=json.dumps([{ "account":"A1","latitude":29.75,"longitude":-95.36,"land_use":"WAREHOUSE"}]).encode()
    source=GenericSocrataSource(fetch=lambda *a:body,binding=_binding())
    first=source.search_page(BBOX,["warehouse_industrial"],1,5)
    with pytest.raises(SourceError,match="repeated the same page"):
        source.search_page(BBOX,["warehouse_industrial"],1,5,first.next_cursor)


def test_authorized_property_has_no_implicit_batch_or_inspection_hold(monkeypatch):
    from app import discovery_runner as runner
    from tests.test_discovery_runner import _prop
    pid=str(uuid4()); seen=[]
    monkeypatch.setattr("app.properties_db.get_property",lambda p:_prop(str(p)))
    monkeypatch.setattr(runner,"_brave_used_for",lambda *a:0)
    monkeypatch.setattr(runner,"_census_used_for",lambda *a:0)
    def start(*args,**kwargs):
        seen.append(kwargs)
        return {"decision":"authorized","run_id":str(uuid4()),"qualification":{"main":"qualified"}}
    monkeypatch.setattr("app.mvp.start_evaluation_audited",start)
    result=runner.evaluate_property("source-first",pid,approved=True,timeout_seconds=12,official_ref={"property_id":pid})
    assert result["decision"]=="authorized"
    assert seen[0]["stop_after_raphael"] is False
    runner.evaluate_property("source-first",pid,approved=True,timeout_seconds=12,official_ref={"property_id":pid},stop_after_raphael=True)
    assert seen[1]["stop_after_raphael"] is True


@pytest.mark.skipif(__import__("os").getenv("GROWUP_PG_INTEGRATION")!="1",reason="isolated PostgreSQL source gate")
def test_live_ranked_child_pages_have_distinct_claims_and_zero_call_replay(monkeypatch):
    import socket
    from tests.test_us_discovery import _find,_qualified_socrata_payload,BBOX
    from tests.test_source_discovery import _require_source_gate,_cleanup_source_gate,_source_gate_usage
    from app.agents.gabriel.sources.source_selection import WorkflowSelectionStore,clear_retained
    from app.agents.gabriel.sources.generic_open_data import source_for_selection
    _require_source_gate()
    monkeypatch.setattr(socket,"getaddrinfo",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,"",("8.8.8.8",443))])
    key="pg-source-pages-"+uuid4().hex
    try:
        finding=_find({"socrata.com":_qualified_socrata_payload(),"arcgis.com":{"data":[]}},
                      assignment_key=key,durable_store=WorkflowSelectionStore())
        child=finding["selection"]["source_plan"][0]
        calls=[]
        def fetch(url,params,timeout):
            calls.append(params["$offset"])
            return json.dumps([{ "account":"A"+params["$offset"],"latitude":29.75,
                "longitude":-95.36,"land_use":"WAREHOUSE"}]).encode()
        source=source_for_selection(child,"generic_socrata",fetch=fetch)
        first=source.search_page(BBOX,["warehouse_industrial"],1,5)
        second=source.search_page(BBOX,["warehouse_industrial"],1,5,first.next_cursor)
        assert first.items[0].external_id != second.items[0].external_id
        clear_retained()
        replay_source=source_for_selection(child,"generic_socrata",fetch=lambda *a:pytest.fail("replay must not transmit"))
        replay=replay_source.search_page(BBOX,["warehouse_industrial"],1,5,first.next_cursor)
        assert replay.items == second.items and calls == ["0","1"]
        assert _source_gate_usage(key)["generic_collection_requests_reserved"] == 2
    finally:
        _cleanup_source_gate(key)


def test_page_claim_contract_replays_and_never_retransmits_uncertain_page():
    from app.agents.gabriel.sources.generic_open_data import GenericSocrataSource
    from tests.test_us_discovery import _binding,BBOX
    rows={}
    class Ledger:
        def claim(self,run,assignment,fingerprint,payload):
            key=payload["page_identity"]
            if key in rows:
                old,done=rows[key]
                if old!=fingerprint:
                    raise SourceError("page replay scope changed")
                return {"status":"completed","outcome":done} if done else {"status":"uncertain"}
            rows[key]=(fingerprint,None)
            return {"status":"winner"}
        def complete(self,run,assignment,fingerprint,outcome):
            rows[outcome["page_identity"]]=(fingerprint,outcome)
    calls=[]
    def fetch(url,params,timeout):
        calls.append(params["$offset"])
        if params["$offset"]=="2":
            raise TimeoutError("test interruption")
        return json.dumps([{ "account":"A"+params["$offset"],"latitude":29.75,
                            "longitude":-95.36,"land_use":"WAREHOUSE"}]).encode()
    source=GenericSocrataSource(fetch=fetch,binding=_binding(assignment_key="finding",claim_run_id=str(uuid4()),selection_digest="retained-selection"),claim_store=Ledger())
    first=source.search_page(BBOX,["warehouse_industrial"],1,5)
    second=source.search_page(BBOX,["warehouse_industrial"],1,5,first.next_cursor)
    replay=source.search_page(BBOX,["warehouse_industrial"],1,5,first.next_cursor)
    assert replay.items==second.items and calls==["0","1"]
    with pytest.raises(TimeoutError):
        source.search_page(BBOX,["warehouse_industrial"],1,5,second.next_cursor)
    with pytest.raises(SourceError,match="uncertain"):
        source.search_page(BBOX,["warehouse_industrial"],1,5,second.next_cursor)
    assert calls==["0","1","2"]


def test_whole_resource_includes_records_outside_old_window(tmp_path,monkeypatch):
    from tests.test_dcad_bulk import _stage,BBOX
    from app.agents.gabriel.sources.dcad_bulk import DcadBulkSource
    _stage(tmp_path);monkeypatch.setenv("DCAD_BULK_DATA_DIR",str(tmp_path))
    source=DcadBulkSource()
    page=source.search_page(BBOX,["high_rise","commercial_property","warehouse_industrial","other"],10,5,whole_resource=True)
    assert len(page.items)==4 and page.exhausted
    assert any(r.street_address=="300 FAR ST" for r in page.items)
    assert any(r.latitude is None for r in page.items)


def test_whole_socrata_resource_does_not_filter_county_window():
    from app.agents.gabriel.sources.generic_open_data import GenericSocrataSource
    from tests.test_us_discovery import _binding,BBOX
    body=json.dumps([{ "account":"A1","latitude":0,"longitude":0,"land_use":"WAREHOUSE"}]).encode()
    source=GenericSocrataSource(fetch=lambda *a:body,binding=_binding())
    page=source.search_page(BBOX,["warehouse_industrial"],2,5,whole_resource=True)
    assert len(page.items)==1 and page.exhausted


def test_whole_arcgis_resource_does_not_send_old_cell_geometry():
    from app.agents.gabriel.sources.generic_open_data import GenericArcGISSource
    from tests.test_us_discovery import _binding,BBOX
    calls=[]
    def fetch(url,params,timeout):
        calls.append(dict(params));return json.dumps({"features":[]}).encode()
    binding=_binding(adapter="generic_arcgis",platform="arcgis",fields=["OBJECTID","address"])
    source=GenericArcGISSource(fetch=fetch,binding=binding)
    page=source.search_page(BBOX,["warehouse_industrial"],2,5,whole_resource=True)
    assert page.exhausted
    assert "geometry" not in calls[0] and calls[0]["orderByFields"]=="OBJECTID ASC"


def test_whole_dcad_streams_pages_without_reloading_full_exchange(tmp_path,monkeypatch):
    from tests.test_dcad_bulk import _stage,BBOX
    from app.agents.gabriel.sources import dcad_bulk
    _stage(tmp_path);monkeypatch.setenv("DCAD_BULK_DATA_DIR",str(tmp_path))
    monkeypatch.setattr(dcad_bulk,"_load_rows",lambda *a:pytest.fail("whole-source paging must stream, not reload the entire file"))
    source=dcad_bulk.DcadBulkSource();cursor=None;ids=[]
    for _ in range(10):
        page=source.search_page(BBOX,["high_rise","commercial_property","warehouse_industrial","other"],1,5,cursor,whole_resource=True)
        ids += [r.external_id for r in page.items]
        if page.exhausted:
            break
        assert page.next_cursor != cursor
        cursor=page.next_cursor
    assert page.exhausted and len(ids)==5  # duplicate retained for shared identity dedup
    assert len(set(ids))==4
