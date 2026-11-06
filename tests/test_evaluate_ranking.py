import csv
import json
import pytest
from scripts.evaluate_ranking import prepare, evaluate, safe_cell, load_sample


def sample(tmp_path):
    path=tmp_path/'sample.json'
    path.write_text(json.dumps({'candidates':[{'source':'journals','title':f'Paper {i}','authors':[],
        'abstract':'A scientific abstract.','url':f'https://example.org/{i}'} for i in range(200)],
        'corpus':[{'title':'Reference','abstract':'Reference abstract','added_date':'2026-01-01'}],
        'keywords':['water']}))
    return path


def test_blind_review_hides_scores_and_blocks_incomplete_labels(tmp_path):
    source=sample(tmp_path);output=tmp_path/'review'
    prepare(source,output,123)
    with (output/'review.csv').open() as handle:
        rows=list(csv.DictReader(handle))
    assert len(rows)==200 and 'score' not in rows[0] and 'method' not in rows[0]
    index=json.loads((output/'private-index.json').read_text())
    order=list(index.values())
    (output/'rankings.json').write_text(json.dumps({'variants':{'baseline':{'order':order}},
                                                  'groups':{key:'journals' for key in order}}))
    with pytest.raises(ValueError,match='Complete all'):evaluate(output)
    with pytest.raises(FileExistsError):prepare(source,output,123)
    for row in rows:row['relevance_0_to_3']='3'
    with (output/'review.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    evaluate(output)
    result=json.loads((output/'labelled-results.json').read_text())['baseline']
    assert result['journals']['ndcg']==1 and result['journals']['precision']==1
    assert result['preprints']['available']==0


def test_sample_identity_count_and_csv_safety(tmp_path):
    path=sample(tmp_path);raw=json.loads(path.read_text());raw['candidates'][1]=raw['candidates'][0]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='unique'):load_sample(path)
    raw['candidates']=raw['candidates'][:10];path.write_text(json.dumps(raw))
    with pytest.raises(ValueError,match='200-300'):load_sample(path)
    assert safe_cell('=SUM(1)')=="'=SUM(1)"
    assert safe_cell('  @unsafe').startswith("'")
    assert safe_cell('Normal title')=='Normal title'
