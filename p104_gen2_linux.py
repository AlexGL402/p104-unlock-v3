#!/usr/bin/env python3
"""Experimental P104-100 Gen2 x4 Linux port. Default: read-only.
Only run --apply on hardware you own and can power-cycle physically.
"""
import argparse, fcntl, json, mmap, os, pathlib, struct, sys, time
from datetime import datetime

PATCHES = [
 (0x8872C,0x0000000f,0x00000006,'FUSE_OVERRIDE'),
 (0x2241C,0x00000081,0x00000081,'PTOP_FS4'),
 (0x8841C,0x00006000,0x00000000,'PRIV_MISC_1'),
 (0x8C2C0,0x00000004,0x00000000,'CYA_0'),
 (0x8C300,0x00000010,0x00000000,'CYA_1'),
 (0x8C040,0x000c0000,0x00040000,'LINK_CONFIG_0'),
 (0x8C1C0,0x000f0000,0x00020000,'LCTRL_2'),
 (0x88610,0x00001001,0x00000001,'VSEC_HIERARCHY'),
]
GPU='0000:03:00.0'
ROOT='0000:00:02.0'
BASE=pathlib.Path('/sys/bus/pci/devices')
LOGDIR=pathlib.Path.home()/'p104-unlock-v3'/'linux-backup'

def read_cfg(bdf,off,size):
    with open(BASE/bdf/'config','rb',buffering=0) as f:
        f.seek(off); data=f.read(size)
    if len(data)!=size or all(v==255 for v in data):
        raise RuntimeError(f'PCI config unavailable: {bdf} +0x{off:x}')
    return int.from_bytes(data,'little')

def write_cfg(bdf,off,size,value):
    with open(BASE/bdf/'config','r+b',buffering=0) as f:
        f.seek(off); n=f.write(value.to_bytes(size,'little'))
    if n!=size: raise RuntimeError('short PCI config write')

def cap_offset(bdf):
    ptr=read_cfg(bdf,0x34,1)&0xfc
    seen=set()
    while ptr and ptr not in seen and ptr<0x100:
        seen.add(ptr)
        if read_cfg(bdf,ptr,1)==0x10: return ptr
        ptr=read_cfg(bdf,ptr+1,1)&0xfc
    raise RuntimeError(f'PCIe capability missing: {bdf}')

def alive():
    return read_cfg(GPU,0,2)==0x10de and read_cfg(GPU,2,2)==0x1b87

def check_alive():
    if not alive(): raise RuntimeError('GPU disappeared: stop; do not access BAR0 again')

def link(bdf,cap):
    return {'cap':read_cfg(bdf,cap+0x0c,4),
            'sta':read_cfg(bdf,cap+0x12,2),
            'cap2':read_cfg(bdf,cap+0x2c,4),
            'ctl2':read_cfg(bdf,cap+0x30,2),
            'ctl':read_cfg(bdf,cap+0x10,2)}

def valid_linkcfg(v): return ((v>>31)&1)!=((v>>29)&1)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group()
    g.add_argument('--probe',action='store_true')
    g.add_argument('--dry-run',action='store_true')
    g.add_argument('--apply',action='store_true')
    p.add_argument('--confirm-risk',action='store_true',help='required with --apply')
    a=p.parse_args()
    if a.apply and not a.confirm_risk: p.error('--apply requires --confirm-risk')
    if os.geteuid()!=0: p.error('run with sudo')
    check_alive()
    parent=(BASE/GPU).resolve().parent.name
    if parent!=ROOT: raise RuntimeError(f'unexpected root port {parent}; expected {ROOT}')
    if read_cfg(ROOT,0,2)!=0x8086: raise RuntimeError('unexpected root vendor')
    gc,rc=cap_offset(GPU),cap_offset(ROOT)
    LOGDIR.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    path=LOGDIR/f'gpu0-gen2-{stamp}.jsonl'
    lock=open('/run/lock/p104-gen2-linux.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def record(kind,**kw):
        line={'ts':datetime.now().isoformat(timespec='milliseconds'),'event':kind,**kw}
        with open(path,'a') as f:
            f.write(json.dumps(line,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
        print(json.dumps(line,ensure_ascii=False),flush=True)
    fd=os.open(str(BASE/GPU/'resource0'),(os.O_RDWR|os.O_SYNC) if a.apply else (os.O_RDONLY|os.O_SYNC))
    try:
        mm=mmap.mmap(fd,0x90000,flags=mmap.MAP_SHARED,
                     prot=(mmap.PROT_READ|mmap.PROT_WRITE) if a.apply else mmap.PROT_READ)
        try:
            def rd(off):
                check_alive()
                return struct.unpack_from('<I',mm,off)[0]
            def wr(off,new,mask):
                check_alive()
                old=rd(off)
                if off==0x8C040 and not valid_linkcfg(new): raise RuntimeError('UNSAFE 0x8C040')
                if old==new:return
                record('before_mmio',offset=hex(off),old=hex(old),new=hex(new))
                struct.pack_into('<I',mm,off,new)
                time.sleep(.02)
                check_alive()
                rb=rd(off)
                record('after_mmio',offset=hex(off),readback=hex(rb))
                if (rb&mask)!=(new&mask): raise RuntimeError(f'MMIO verify failed at {off:x}')
            def set_tls(bdf,cap):
                check_alive()
                old=read_cfg(bdf,cap+0x30,2)
                new=(old&~15)|2
                if old==new:return
                record('before_tls',bdf=bdf,old=hex(old),new=hex(new))
                write_cfg(bdf,cap+0x30,2,new)
                check_alive()
                rb=read_cfg(bdf,cap+0x30,2)
                record('after_tls',bdf=bdf,readback=hex(rb))
                if (rb&15)!=2: raise RuntimeError('TLS readback mismatch')
            record('baseline',gpu=GPU,root=ROOT,gpu_link=link(GPU,gc),root_link=link(ROOT,rc),
                   registers={hex(o):hex(rd(o)) for o,_,_,_ in PATCHES})
            for off,mask,val,name in PATCHES:
                old=rd(off);new=(old&(~mask&0xffffffff))|(val&mask)
                if off==0x8C040 and not valid_linkcfg(new):raise RuntimeError('UNSAFE LINKCFG')
                record('plan',name=name,offset=hex(off),old=hex(old),new=hex(new))
            if not a.apply:
                record('plan_pci',gpu_tls=2,root_tls=2,retrain_root=ROOT,
                       note='No hardware writes; x16 width arm excluded')
                return
            # No auto retries or width x16 arm. One controlled attempt only.
            for off,mask,val,name in PATCHES:
                old=rd(off);new=(old&(~mask&0xffffffff))|(val&mask)
                wr(off,new,mask)
            after_live=link(GPU,gc)
            record('after_live',gpu_link=after_live)
            if (after_live['cap2'] & 0x6) != 0x6:
                record('abort',reason='LNKCAP2 Gen2 not latched; skipping TLS/retrain')
                raise RuntimeError('GPU does not advertise Gen2; PCIe retrain cancelled')
            set_tls(GPU,gc)
            set_tls(ROOT,rc)
            check_alive()
            old=read_cfg(ROOT,rc+0x10,2)
            record('before_retrain',root=ROOT,linkctl=hex(old))
            write_cfg(ROOT,rc+0x10,2,old|(1<<5))
            time.sleep(1.0)
            check_alive()
            state=link(GPU,gc)
            record('result',gpu_link=state,root_link=link(ROOT,rc),
                   success=((state['sta']&15)==2 and ((state['sta']>>4)&63)==4))
        finally:mm.close()
    finally:os.close(fd)
    print('Log:',path)

if __name__=='__main__':
    try:main()
    except Exception as e:
        print('STOP:',type(e).__name__,str(e),file=sys.stderr)
        sys.exit(1)
