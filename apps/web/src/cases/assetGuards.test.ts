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
  it("解析S3 filter selection并保持完整Case集合", () => {
    const parsed=toAssetSelection({selection_id:"s-filter",schema_version:1,action:"archive",mode:"filter",...scope,created_at:"c",expires_at:"e",counts:{selected:2,eligible:1,excluded:1,cases:2,folders:0},root:null,preview_items:[{resource_type:"case",id:"a",rev:1,state:"active",name:"A",parent_id:null,folder_id:null,outcome:"eligible",code:null}],excluded_items:[{resource_type:"case",id:"b",rev:2,state:"archived",name:"B",parent_id:null,folder_id:null,outcome:"excluded",code:"revision_or_state_conflict"}]});
    expect(parsed.mode).toBe("filter");
    expect([...parsed.preview_items,...parsed.excluded_items].map((item)=>item.id)).toEqual(["a","b"]);
  });
  it("目录按eligible上限保留大量excluded，普通Case仍按selected限制", () => {
    const excluded=Array.from({length:500},(_,index)=>({resource_type:"case",id:`c-${index}`,rev:1,state:"archived",name:`C${index}`,parent_id:null,folder_id:null,outcome:"excluded",code:"already_archived"}));
    expect(toAssetSelection({selection_id:"sf",schema_version:1,action:"folder_archive",mode:"folder",...scope,created_at:"c",expires_at:"e",counts:{selected:501,eligible:1,excluded:500,cases:500,folders:1},root:{resource_type:"folder",id:"root",expected_rev:1},preview_items:[{resource_type:"folder",id:"root",rev:1,state:"active",name:"根",parent_id:null,folder_id:null,outcome:"eligible",code:null}],excluded_items:excluded}).counts.selected).toBe(501);
    const tooMany=[...excluded,{resource_type:"case",id:"extra",rev:1,state:"active",name:"E",parent_id:null,folder_id:null,outcome:"eligible",code:null}];
    expect(()=>toAssetSelection({selection_id:"sc",schema_version:1,action:"archive",mode:"filter",...scope,created_at:"c",expires_at:"e",counts:{selected:501,eligible:1,excluded:500,cases:501,folders:0},root:null,preview_items:[tooMany.at(-1)],excluded_items:excluded})).toThrow(ContractError);
  });
});
