"""Local-only decryption and a neutral human evaluation CSV. No keys printed."""
import argparse
import csv
import json
from pathlib import Path
import logic as L

def write_deliverables(envelope,private_key,output,protocol,original=None):
    result=L.prior.unseal(envelope,private_key,expected_experiment=L.ROUND,expected_protocol_sha256=L.CONTEXT_SHA)
    if result.get('protocol_sha256') and result['protocol_sha256']!=L.digest(L.validate_protocol(protocol)):
        raise L.Stop('decrypted_private_protocol_mismatch')
    if result.get('recovery_id') and result.get('status')=='completed':
        if original is None:raise L.Stop('local_original_results_required_for_frozen_pairing')
        from recover import attach_original_pairing
        result=attach_original_pairing(result,original)
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    hidden=output/'paired_results.local.json';L.adapter.write(hidden,result)
    rows=result.get('blind_rows',[])
    summary={'status':result.get('status'),'hidden_results':str(hidden),'budget':result.get('cumulative_budget')}
    if result.get('status')!='completed':
        return summary|{'phase':result.get('phase'),'reason':result.get('reason')}
    candidates=result.get('candidates',[]);mapping=result.get('blind_to_scored_id',{})
    if len(rows) not in L.SIZES or len(rows)!=len(candidates) or set(mapping)!=set(x['id'] for x in rows) or set(mapping.values())!=set(x['id'] for x in candidates):
        raise L.Stop('blind_mapping_or_sample_size_mismatch')
    allowed={'id','title','link','abstract_excerpt','relevance','reason'}
    if any(set(row)!=allowed or row['relevance'] or row['reason'] for row in rows):
        raise L.Stop('blind_pack_contains_non_neutral_fields')
    path=output/'blind_evaluation.csv'
    def safe(value):
        value=str(value)
        return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f)
        writer.writerow(['编号','论文标题','论文链接','摘要节选（最多25词；完整摘要见链接）','相关性（0不相关/1边缘/2有用/3必读）','可选理由'])
        for row in rows:writer.writerow([safe(row[k]) for k in ('id','title','link','abstract_excerpt','relevance','reason')])
    path.chmod(0o600)
    return summary|{'blind_pack':str(path),'rows':len(rows)}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('encrypted','private-key','protocol','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--original-results',type=Path)
    args=parser.parse_args()
    summary=write_deliverables(json.loads(args.encrypted.read_text()),args.private_key.read_bytes(),args.output,json.loads(args.protocol.read_text()),json.loads(args.original_results.read_text()) if args.original_results else None)
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
