"""Bounded, failure-only retention of verified canary framebuffer closures."""
import json
import os
import uuid
from kptas.platform_sync import sync_dir
from kptas.types import sha256


class CanaryFrames:
    LIMIT=64*1024*1024

    def __init__(self,bundle,start,end):
        self.bundle,self.start,self.end=bundle,start,end
        self.blobs={};self.candidates=[];self.errors=[];self.size=0

    def retain(self,report,blobs):
        from kptas.backend.shim import gl_children,validate_framebuffer
        incoming=dict(blobs)
        for key,desc in blobs:
            if desc[:4]!=b'KPGF':continue
            try:
                children=gl_children(desc)
                closure={key:desc}
                for child in children:
                    name=child.hex()
                    data=incoming.get(name,self.blobs.get(name))
                    if data is None:data=self.bundle.file(child)
                    if sha256(data)!=child:raise ValueError('framebuffer child digest mismatch')
                    closure[name]=data
                validate_framebuffer(desc,[closure[child.hex()] for child in children])
                if sha256(desc).hex()!=key:raise ValueError('framebuffer descriptor digest mismatch')
                added=sum(len(data) for name,data in closure.items() if name not in self.blobs)
                if self.size+added>self.LIMIT:raise ValueError('canary framebuffer evidence exceeds64MiB bound')
                self.blobs.update(closure);self.size+=added
                self.candidates.append({'descriptor':key,'children':[child.hex() for child in children],
                                        'batch_end_tick':report.tick})
            except Exception as exc:
                self.errors.append(f'{key}: {type(exc).__name__}: {exc}')

    def persist(self,failure):
        parent=self.bundle.root/'forensics'
        parent.mkdir(exist_ok=True)
        directory=parent/('canary-'+uuid.uuid4().hex)
        directory.mkdir()
        def write(name,data):
            with (directory/name).open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
        for key,data in self.blobs.items():write(key,data)
        manifest={'schema':'kptas-canary-frame-evidence/1','canary_from_tick':self.start,
                  'canary_to_tick':self.end,'first_divergence':getattr(failure,'tick',None),
                  'failure':str(failure),'candidates':self.candidates,'errors':self.errors,
                  'association':'Candidates belong to replay batches; batch_end_tick is not individual frame tick.'}
        write('manifest.json',json.dumps(manifest,indent=2).encode()+b'\n')
        sync_dir(directory);sync_dir(parent);sync_dir(self.bundle.root)
        return directory
