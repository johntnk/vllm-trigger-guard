"""Nested synthetic dictionaries, fixed anchors, no private/business content."""
import argparse
import hashlib
import json
from pathlib import Path
import random

ANCHORS=['花朵','ZXQ_BLOCK_ALPHA','ZXQ_BLOCK_BETA','紫色灯塔']
NOTICE='\n\n【提示】我不能继续了。'
FAMILIES=['representative','prefix_heavy_near_miss','long_phrase']
SIZES=[16,128,1024,4096,10000,50000,100000]


def policy(n, family='representative', seed=42):
    if family not in FAMILIES or n<4:
        raise ValueError('nonempty corpus requires at least four anchors')
    rng=random.Random(seed)
    words=list(ANCHORS)
    for i in range(n-len(words)):
        # Permutation of a fixed-width integer preserves uniqueness and nesting.
        key=(i*49999+seed*7919)%1000003
        if family=='prefix_heavy_near_miss':
            word=f'ZXQ_COMMON_PREFIX_SHARED_PATH_{key:07d}_END'
        elif family=='long_phrase':
            word=f'ZXQ begin synthetic restricted sentence {key:07d} '+('silent river carries distant light '*3)+'END'
        else:
            templates=[f'ZXQ_{key:07d}_END',f'保留标记甲{key:07d}乙',f'ZXQ synthetic phrase {key:07d} END']
            word=templates[rng.randrange(len(templates))]
        words.append(word)
    return {'policy_version':f'dict-v2.1-{family}-{seed}-{n}',
            'notice_id':'fixed-v1','notice':NOTICE,'match_semantics':'literal_substring',
            'rules':[{'rule_id':f'r{i:06d}','text':w,'priority':0} for i,w in enumerate(words)]}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--sizes',type=int,nargs='+',default=SIZES)
    parser.add_argument('--seeds',type=int,nargs='+',default=[42,43,44])
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    manifest=[]
    for family in FAMILIES:
        for seed in args.seeds:
            for n in args.sizes:
                data=policy(n,family,seed)
                raw=json.dumps(data,ensure_ascii=False,sort_keys=True).encode()
                name=f'{family}_{seed}_{n}.json'
                (args.out/name).write_bytes(raw)
                words=[r['text'] for r in data['rules']]
                manifest.append(dict(path=name,seed=seed,family=family,N=n,
                    raw_rule_count=n,unique_literal_rules=len(set(words)),
                    policy_sha256=hashlib.sha256(raw).hexdigest(),total_characters=sum(map(len,words)),
                    max_characters=max(map(len,words)),synthetic=True))
    (args.out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({'policies':len(manifest),'out':str(args.out)}))


if __name__=='__main__':main()
