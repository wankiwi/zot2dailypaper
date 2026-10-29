"""Local only. Decrypt without printing keys, rankings or the hidden mapping."""
import argparse
import csv
import json
from pathlib import Path
from common import unseal

def write_deliverables(envelope,private_key,output):
    result=unseal(envelope,private_key)
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    hidden=output/'paired_results.local.json'
    hidden.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');hidden.chmod(0o600)
    blind=result.get('blind_rows',[])
    if result.get('status')=='completed' and len(blind)==24:
        path=output/'blind_evaluation.csv'
        def safe(value):
            value=str(value)
            return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f)
            writer.writerow(['编号','论文标题','论文链接','摘要节选（最多25词；完整摘要见链接）','相关性（0不相关/1边缘/2有用/3必读）','可选理由'])
            for row in blind:writer.writerow([safe(row[key]) for key in ('id','title','link','abstract_excerpt','relevance','reason')])
        path.chmod(0o600)
        return {'status':result['status'],'blind_pack':str(path),'hidden_results':str(hidden),'rows':len(blind),'budget':result.get('cumulative_budget')}
    return {'status':result.get('status'),'hidden_results':str(hidden),'reason':result.get('reason'),'phase':result.get('phase')}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--encrypted',type=Path,required=True);parser.add_argument('--private-key',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    summary=write_deliverables(json.loads(args.encrypted.read_text()),args.private_key.read_bytes(),args.output)
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
