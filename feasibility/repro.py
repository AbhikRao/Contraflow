import random, sys, json
sys.path.insert(0,'.')
from falkordb import FalkorDB
from tests.test_maxflow_correctness import random_graph
from feasibility.flowlib import *
def case(k):
    rng = random.Random(20261015)
    for i in range(k+1):
        nodes, edges = random_graph(rng)
        src = rng.sample(nodes[:10], rng.choice([1, 2, 3])); dst = rng.sample(nodes[-10:], rng.choice([1, 2, 3]))
        mode = i % 3
        if mode == 0: edges = [e for e in edges if rng.random() > 0.15]
        elif mode == 1: edges = [Edge(e.eid, e.u, e.v, 0.0 if rng.random() < 0.15 else e.cap) for e in edges]
        else: edges = [Edge(e.eid, e.u, e.v, e.cap * rng.choice([0.5, 1.0, 1.0, 1.5, 2.0])) for e in edges]
    return edges, src, dst
def solve(port, edges, src, dst, name='repro'):
    norm,_=normalize_parallel(edges)
    g=FalkorDB(port=port).select_graph(name)
    try: g.delete()
    except Exception: pass
    load_graph(g, norm)
    try: v,f,dt=falkor_maxflow(g,src,dst)
    except Exception as ex: v,f,dt=str(ex),{},0
    g.delete(); return v,f,dt,norm
if __name__=='__main__':
    k=int(sys.argv[1]); port=int(sys.argv[2])
    edges,src,dst=case(k)
    v,f,dt,norm=solve(port,edges,src,dst)
    print('falkor',v,'nx',nx_maxflow(edges,src,dst),'dt',round(dt,3),'nedges',len(norm), 'srcs',src,'dst',dst)
    # variants
    for lbl,ed,s,d in [('int caps',[Edge(e.eid,e.u,e.v,int(e.cap)) for e in edges],src,dst),
                      ('single src', edges, src[:1], dst),
                      ('single dst', edges, src, dst[:1]),
                      ('drop zero', [e for e in edges if e.cap>0], src, dst)]:
        v,f,dt,_=solve(port,ed,s,d); print(lbl,'falkor',v,'nx',nx_maxflow(ed,s,d),'dt',round(dt,3))
    # per-source / per-sink
    for s in src:
        for t in dst:
            v,f,dt,_=solve(port,edges,[s],[t]); print('pair',s,t,'falkor',v,'nx',nx_maxflow(edges,[s],[t]))
    json.dump({'edges':[e.__dict__ for e in edges],'src':src,'dst':dst},open(f'results/repro_case{k}.json','w'))
