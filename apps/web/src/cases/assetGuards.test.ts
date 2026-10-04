import { describe, expect, it } from "vitest";
import { ContractError } from "../api/guards";
import { toAssetOperation, toAssetSelection } from "./assetGuards";

const scope = { workspace_id: "w", project_id: "p", principal_id: "u" };
describe("S2资产网络合同", () => {
  it("解析完整selection与completed信封并检查数量守恒", () => {
    expect(toAssetSelection({ selection_id:"s",schema_version:1,action:"archive",mode:"explicit",...scope,created_at:"c",expires_at:"e",counts:{selected:1,eligible:1,excluded:0,cases:1,folders:0},root:null,preview_items:[{resource_type:"case",id:"c",rev:2,state:"active",name:"用例",parent_id:null,folder_id:null,outcome:"eligible",code:null}],excluded_items:[] }).selection_id).toBe("s");
    const operation=toAssetOperation({operation_id:"o",operation_key:"k",action:"archive",...scope,result_schema_version:1,created_at:"c",result:{result_kind:"completed",selection_id:"s",root:null,counts:{input:1,succeeded:1,no_change:0,conflict:0,failed:0},items:[{resource_type:"case",id:"c",outcome:"succeeded",code:null,message:"已归档",new_rev:3,asset:{id:"c",resource_type:"case",name:"用例",rev:3,state:"archived",folder_id:null,parent_id:null,archived_at:null}}],members:[]}});
    expect(operation.result.result_kind).toBe("completed");
  });
  it("拒绝假rejected和不守恒结果",()=>{
    expect(()=>toAssetOperation({operation_id:"o",operation_key:"k",action:"archive",...scope,result_schema_version:1,created_at:"c",result:{result_kind:"rejected",selection_id:"s",code:"x",message:"x",no_asset_changes:false,conflicts:[]}})).toThrow(ContractError);
    expect(()=>toAssetOperation({operation_id:"o",operation_key:"k",action:"archive",...scope,result_schema_version:1,created_at:"c",result:{result_kind:"completed",selection_id:"s",root:null,counts:{input:2,succeeded:1,no_change:0,conflict:0,failed:0},items:[],members:[]}})).toThrow(ContractError);
  });
  it("允许不可见excluded隐藏元数据，但eligible仍必须有rev/state", () => {
    const base={selection_id:"s",schema_version:1,action:"archive",mode:"explicit",...scope,created_at:"c",expires_at:"e",counts:{selected:1,eligible:0,excluded:1,cases:1,folders:0},root:null,preview_items:[],excluded_items:[{resource_type:"case",id:"hidden",rev:null,state:null,name:null,parent_id:null,folder_id:null,outcome:"excluded",code:"not_found_or_inaccessible"}]};
    expect(toAssetSelection(base).excluded_items[0].name).toBeNull();
    expect(()=>toAssetSelection({...base,counts:{...base.counts,eligible:1,excluded:0},preview_items:[{...base.excluded_items[0],outcome:"eligible",code:null}],excluded_items:[]})).toThrow(ContractError);
  });
  it("合法no_change与conflict允许new_rev为空且保留当前asset rev", () => {
    for (const outcome of ["no_change", "conflict"] as const) {
      const counts={input:1,succeeded:0,no_change:outcome==="no_change"?1:0,conflict:outcome==="conflict"?1:0,failed:0};
      expect(toAssetOperation({operation_id:"o",operation_key:"k",action:"move",...scope,result_schema_version:1,created_at:"c",result:{result_kind:"completed",selection_id:"s",root:null,counts,items:[{resource_type:"case",id:"c",outcome,code:null,message:"未变化",new_rev:null,asset:{id:"c",resource_type:"case",name:"用例",rev:2,state:"active",folder_id:null,parent_id:null,archived_at:null}}],members:[]}}).result.result_kind).toBe("completed");
    }
  });
});
