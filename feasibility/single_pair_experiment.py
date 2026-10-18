"""Does FalkorDB maxFlow also err with exactly one source and one sink?"""
import sys, random, json
sys.path.insert(0,'.')
from collections import defaultdict
from feasibility.repro import solve
from feasibility.flowlib import *
port=int(sys.argv[1]); N=int(sys.argv[2]); seed=int(sys.argv[3]) if len(sys.argv)>3 else 11
rng=random.Random(seed); stats=defaultdict(int); fails=[]
for k in range(N):
    n=rng.choice([10,20,40,80,150]); m=n*rng.choice([2,3,4])
    nodes=[f"n{i}" for i in range(n)]
    edges=[]
    for i in range(m):
        u,v=rng.sample(nodes,2); edges.append(Edge(f"e{i}",u,v,float(rng.choice([150,300,450,600,900,1200,1800,2400,3600,7200]))))
    s=edges[0].u; t=edges[-1].v
    if s==t: continue
    ref=nx_maxflow(edges,[s],[t])
    v,f,dt,_=solve(port,edges,[s],[t],'pair_exp')
    ok=isinstance(v,float) and abs(v-ref)<=1e-6*max(1,ref)
    stats['ok' if ok else 'bad']+=1
    if not ok: fails.append({'k':k,'n':n,'m':m,'v':v,'ref':ref,'edges':[e.__dict__ for e in edges],'s':s,'t':t})
print(port, dict(stats)); print([(x['k'],x['n'],x['v'],x['ref']) for x in fails[:8]])
json.dump({'port':port,'cases':N,'stats':stats,'failures':fails},open(f'results/single_pair_experiment_{port}.json','w'))
