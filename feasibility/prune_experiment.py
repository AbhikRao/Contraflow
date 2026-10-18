"""Hypothesis test: FalkorDB maxFlow errs when flow can enter nodes that cannot
reach a sink (dead ends). Prune to the source->sink 'live core' and re-test."""
import sys, random, json
sys.path.insert(0,'.')
from collections import defaultdict, deque
from feasibility.repro import solve
from feasibility.flowlib import *
def reach(adj, starts):
    seen=set(starts); dq=deque(starts)
    while dq:
        x=dq.popleft()
        for y in adj[x]:
            if y not in seen: seen.add(y); dq.append(y)
    return seen
def live_core(edges, src, dst):
    f=defaultdict(list); b=defaultdict(list)
    for e in edges:
        if e.cap>0: f[e.u].append(e.v); b[e.v].append(e.u)
    live=reach(f,src)&reach(b,dst)
    return [e for e in edges if e.u in live and e.v in live and e.cap>0]
port=int(sys.argv[1]); N=int(sys.argv[2])
rng=random.Random(7)
stats=defaultdict(int); fails=[]
for k in range(N):
    n=rng.choice([20,40,80,150]); m=n*rng.choice([2,3,4])
    nodes=[f"n{i}" for i in range(n)]
    edges=[]
    for i in range(m):
        u,v=rng.sample(nodes,2); edges.append(Edge(f"e{i}",u,v,float(rng.choice([150,300,450,600,900,1200,1800,2400,3600,7200]))))
    src=rng.sample(nodes[:n//4],rng.choice([1,2,3,4])); dst=rng.sample(nodes[-n//4:],rng.choice([1,2,3]))
    src=[s for s in src if any(e.u==s or e.v==s for e in edges)] or [edges[0].u]
    dst=[t for t in dst if any(e.u==t or e.v==t for e in edges)] or [edges[-1].v]
    ref=nx_maxflow(edges,src,dst)
    for lbl,es in (('raw',edges),('pruned',live_core(edges,src,dst))):
        if not es: v=0.0
        else:
            s2=[s for s in src if any(e.u==s or e.v==s for e in es)]; t2=[t for t in dst if any(e.u==t or e.v==t for e in es)]
            if not s2 or not t2: v=0.0
            else: v,f,dt,_=solve(port,es,s2,t2,'prune_exp')
        ok = isinstance(v,float) and abs(v-ref)<=1e-6*max(1,ref)
        stats[f'{lbl}_ok' if ok else f'{lbl}_bad']+=1
        if not ok and lbl=='pruned': fails.append({'k':k,'n':n,'m':m,'v':v,'ref':ref})
print(port, dict(stats)); print('pruned failures',fails[:5])
json.dump({'port':port,'cases':N,'stats':stats,'pruned_failures':fails},open(f'results/prune_experiment_{port}.json','w'),indent=1)
