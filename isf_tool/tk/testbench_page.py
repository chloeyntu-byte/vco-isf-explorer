"""Shared A/B/C preparation page; callbacks are marshalled by the app worker."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from .. import api
from ..core.testbench import digest, read, write
from ..core.offsets import expand_offsets
from ..core.component_selection import selection_request


class TestbenchPage(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,padding=16);self.app=app;self.prepared=None;self.request={};self.job=None;self._poll=None;self.revision=0
        self.columnconfigure(1,weight=1);self.rowconfigure(7,weight=1)
        self.session_handle=None
        self.entry=tk.StringVar(value='cell');self.tbgen=tk.StringVar(value='');self.cds=tk.StringVar();self.lib=tk.StringVar();self.cell=tk.StringVar();self.view=tk.StringVar(value='schematic')
        self.peer=tk.StringVar();self.gm=tk.StringVar();self.p=tk.StringVar();self.n=tk.StringVar();self.frequency=tk.StringVar();self.offsets=tk.StringVar(value='10000');self.status=tk.StringVar(value='Choose a source and load a request, or browse a library for a cell. Preparation is read-only.')
        self.complementary = tk.BooleanVar(value=False)
        self.core_mode = tk.StringVar(value='complementary')
        self.second_gm = tk.StringVar()
        self.second_peer = tk.StringVar()
        entry=ttk.Frame(self);entry.grid(row=0,column=0,columnspan=3,sticky='ew')
        for label,value in [('A · Design Lab','design-lab'),('B · Existing Maestro','maestro'),('C · New cell','cell')]:
            ttk.Radiobutton(entry,text=label,value=value,variable=self.entry,command=self.change_entry).pack(side='left',padx=8)
        ttk.Button(entry,text='Load request…',command=self.load_request).pack(side='left',padx=8)
        ttk.Button(entry,text='Save sample request…',command=self.save_sample).pack(side='left')
        self.source = tk.StringVar()
        self.source_test = tk.StringVar()
        self.source_controls = ttk.Frame(self)
        self.source_controls.grid(row=10,column=0,columnspan=3,sticky='ew',pady=4)
        self.source_label = ttk.Label(self.source_controls)
        self.source_label.pack(side='left',padx=5)
        ttk.Entry(self.source_controls,textvariable=self.source,width=42).pack(side='left')
        ttk.Button(self.source_controls,text='Choose directory…',command=self.choose_source).pack(side='left',padx=4)
        self.test_controls = ttk.Frame(self.source_controls)
        ttk.Label(self.test_controls,text='Source test').pack(side='left')
        ttk.Entry(self.test_controls,textvariable=self.source_test,width=15).pack(side='left',padx=4)
        for row,label,var in [(1,'TB Generator path',self.tbgen),(2,'Private cds.lib',self.cds)]:
            ttk.Label(self,text=label).grid(row=row,column=0,sticky='w');ttk.Entry(self,textvariable=var).grid(row=row,column=1,sticky='ew')
        self.browse_button = ttk.Button(self,text='Browse libraries / cells',command=self.browse)
        self.browse_button.grid(row=2,column=2,padx=5)
        lcv=ttk.Frame(self);lcv.grid(row=3,column=0,columnspan=3,sticky='ew')
        self.boxes={}
        for label,var in [('Library',self.lib),('Cell',self.cell),('View',self.view)]:
            ttk.Label(lcv,text=label).pack(side='left');box=ttk.Combobox(lcv,textvariable=var,width=22);box.pack(side='left',padx=5);self.boxes[label]=box
        self.boxes['Library'].bind('<<ComboboxSelected>>',self.library_changed)
        self.boxes['Cell'].bind('<<ComboboxSelected>>',self.cell_changed)
        ttk.Button(lcv,text='Read ports',command=self.describe).pack(side='left',padx=5)
        choices=ttk.Frame(self);choices.grid(row=4,column=0,columnspan=3,sticky='ew')
        for label,var in [('Target gm',self.gm),('Output P',self.p),('Output N',self.n)]:
            ttk.Label(choices,text=label).pack(side='left');box=ttk.Combobox(choices,textvariable=var,width=22);box.pack(side='left',padx=4);self.boxes[label]=box
        ttk.Button(choices,text='Browse gm hierarchy',command=self.select_gm).pack(side='left')
        core = ttk.Frame(self)
        core.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(8, 0))
        ttk.Checkbutton(core, text='Two-device analysis', variable=self.complementary,
                        command=self.update_core_controls).pack(anchor='w')
        self.core_fields = ttk.Frame(core)
        ttk.Label(self.core_fields, text='Second device').grid(row=0, column=0, sticky='w')
        self.second_box = ttk.Combobox(self.core_fields, textvariable=self.second_gm, width=24)
        self.second_box.grid(row=0, column=1, padx=6)
        ttk.Button(self.core_fields, text='Browse devices',
                   command=lambda: self.select_gm(self.second_gm)).grid(row=0, column=2)
        ttk.Label(self.core_fields, text='Symmetric peer (optional)').grid(row=0, column=3, padx=(12, 0))
        self.second_peer_entry = ttk.Entry(self.core_fields, textvariable=self.second_peer, width=24)
        self.second_peer_entry.grid(row=0, column=4, padx=6)
        modes = ttk.Frame(self.core_fields)
        modes.grid(row=1, column=0, columnspan=5, sticky='w', pady=4)
        for label, mode in (('Complementary core', 'complementary'), ('Asymmetric pair', 'asymmetric_pair')):
            ttk.Radiobutton(modes, text=label, value=mode, variable=self.core_mode,
                            command=self.update_core_controls).pack(side='left')
        numerics=ttk.Frame(self);numerics.grid(row=6,column=0,columnspan=3,sticky='ew',pady=8)
        for label,var in [('Independent peer (optional)',self.peer),('Known fguess (Hz)',self.frequency),('Offsets (e.g. 10k, 1M)',self.offsets)]:
            ttk.Label(numerics,text=label).pack(side='left');ttk.Entry(numerics,textvariable=var,width=20).pack(side='left',padx=6)
        self.table=ttk.Treeview(self,columns=('role','net','value'),show='tree headings',height=6)
        self.table.heading('#0',text='Port / plan');self.table.heading('role',text='Role (double-click to confirm)');self.table.heading('net',text='Connection / retained');self.table.heading('value',text='Value / reason')
        self.table.column('#0',width=180);self.table.column('role',width=100);self.table.column('net',width=120);self.table.column('value',width=400)
        self.table.grid(row=7,column=0,columnspan=3,sticky='nsew');self.table.bind('<Double-1>',self.edit_port)
        actions=ttk.Frame(self);actions.grid(row=8,column=0,columnspan=3,sticky='ew',pady=8)
        ttk.Button(actions,text='Add decimal register…',command=self.add_register).pack(side='left',padx=4)
        ttk.Button(actions,text='Advanced configuration…',command=self.edit_request).pack(side='left')
        ttk.Button(actions,text='Prepare',command=self.prepare).pack(side='left',padx=8)
        self.start_button=ttk.Button(actions,text='Start analysis',style='Primary.TButton',command=self.start,state='disabled');self.start_button.pack(side='left')
        session_actions=ttk.Frame(self);session_actions.grid(row=9,column=0,columnspan=3,sticky='ew')
        self.session_button=ttk.Button(session_actions,text='Add test in current Maestro and analyze',command=self.start_session,state='disabled')
        self.session_button.pack(side='left',padx=6)
        self.review_button=ttk.Button(session_actions,text='Review plan',command=lambda:self.review_settings(self.prepared),state='disabled')
        self.review_button.pack(side='left')
        ttk.Label(self,textvariable=self.status,wraplength=1000).grid(row=11,column=0,columnspan=3,sticky='ew')
        for var in (self.source,self.source_test,self.entry,self.gm,self.peer,self.second_gm,self.second_peer,self.p,self.n,self.frequency,self.offsets,self.cds,self.lib,self.cell,self.view,self.tbgen):var.trace_add('write',lambda *args:self.invalidate())
        self.bind('<Destroy>',self.destroyed,add='+')
        self.update_source_controls()

    def update_source_controls(self):
        entry = self.entry.get()
        if entry == 'cell':
            self.source_controls.grid_remove()
            self.browse_button.grid()
        else:
            self.source_controls.grid(row=10, column=0, columnspan=3, sticky='ew', pady=4)
            self.browse_button.grid_remove()
            self.source_label.configure(text='Generated directory' if entry == 'design-lab' else 'Maestro setup')
        if entry == 'maestro':
            self.test_controls.pack(side='left')
        else:
            self.test_controls.pack_forget()

    def change_entry(self):
        self.request = {}
        self.source.set('')
        self.source_test.set('')
        self.table.delete(*self.table.get_children())
        self.update_source_controls()
        self.invalidate()

    def choose_source(self):
        path = filedialog.askdirectory(parent=self, title=self.source_label.cget('text'))
        if path:
            self.source.set(path)

    def save_sample(self):
        entry = self.entry.get()
        dut = dict(lib='example_lib', cell='example_vco', view='schematic')
        target = dict(lib='example_lib' if entry == 'maestro' else 'example_isf',
                      cell='example_tb', view='maestro', test='HB_ISF')
        payload = dict(dut=dut, cds_lib='/path/to/cds.lib', maestro=target,
                       library_path='/path/to/example_library')
        if entry == 'design-lab':
            payload['generated_dir'] = '/path/to/generated_design'
        elif entry == 'maestro':
            target['source_test'] = 'VCO'
        path = filedialog.asksaveasfilename(parent=self, title='Save sample request',
                                         defaultextension='.json', initialfile=entry + '-request.json')
        if path:
            write(path, payload)

    def update_core_controls(self):
        self.second_peer_entry.state(['disabled' if self.core_mode.get() == 'asymmetric_pair' else '!disabled'])
        if self.complementary.get():
            self.core_fields.pack(fill='x')
        else:
            self.core_fields.pack_forget()
        self.invalidate()

    def destroyed(self,event):
        if event.widget is self:
            if self._poll:self.after_cancel(self._poll);self._poll=None
            if self.session_handle:
                bridge=Path(self.session_handle['bridge_dir'])
                if bridge.is_dir() and not (bridge/'native-status.txt').exists():(bridge/'cancelled').write_text('ISF GUI closed\n')

    def invalidate(self):
        self.revision+=1
        if self.prepared is not None:
            self.table.delete(*self.table.get_children())
        self.prepared=None
        if hasattr(self,'start_button'):self.start_button.configure(state='disabled')
        if hasattr(self,'session_button'):self.session_button.configure(state='disabled')
        if hasattr(self,'review_button'):self.review_button.configure(state='disabled')
        self.status.set({
            'design-lab': 'Select a generated Design Lab directory and enter the DUT.',
            'maestro': 'Select an existing Maestro setup and enter its source test.',
            'cell': 'Load a request or browse for a cell, then confirm its ports.',
        }[self.entry.get()])

    def load_request(self,path=None):
        path=path or filedialog.askopenfilename(parent=self,title='ISF request JSON')
        if not path:return
        try:
            self.request=read(path);self.tbgen.set(self.request.get('tbgen_root',''));tb=self.request.get('tbgen_spec',self.request)
            maestro = tb.get('maestro') or {}
            if self.entry.get() == 'design-lab':
                self.source.set(self.request.get('generated_dir') or (tb.get('source') or {}).get('generated_dir') or '')
            elif self.entry.get() == 'maestro':
                self.source_test.set(maestro.get('source_test', ''))
                if tb.get('library_path') and maestro.get('cell') and maestro.get('view'):
                    self.source.set(str(Path(tb['library_path']) / maestro['cell'] / maestro['view']))
            self.cds.set(tb.get('cds_lib',''));dut=tb.get('dut',{})
            self.lib.set(dut.get('lib',''));self.cell.set(dut.get('cell',''));self.view.set(dut.get('view','schematic'))
            self.status.set('Request loaded. Confirm outputs, gm, supplies, bias, and codes.')
            self.show_ports(tb.get('ports',[]))
        except Exception as exc:self.app._show_error(exc)

    def load_spec(self,path):
        s=read(path);self.entry.set({'design_lab':'design-lab','maestro':'maestro','oa_cell':'cell'}[s['source']['kind']]);tb=s['extensions']['tbgen_spec']
        self.request=dict(tbgen_spec=tb,topology_file=s['extensions'].get('topology_file'));self.tbgen.set(s['extensions'].get('tbgen_root') or '')
        source = s['source']
        self.source.set(source.get('generated_dir') or source.get('maestro_dir') or '')
        self.source_test.set(tb['maestro'].get('source_test', ''))
        self.cds.set(tb['cds_lib']);self.lib.set(tb['dut']['lib']);self.cell.set(tb['dut']['cell']);self.view.set(tb['dut']['view'])
        self.peer.set(','.join(s['gm'].get('peer_instances',[])));self.gm.set(s['gm']['target_instance'] or '');self.p.set(s['output']['positive'] or '');self.n.set(s['output']['negative'] or '')
        complementary = s['gm'].get('complementary')
        self.complementary.set(complementary is not None)
        self.core_mode.set((complementary or {}).get('mode', 'complementary'))
        self.second_gm.set((complementary or {}).get('target_instance') or '')
        self.second_peer.set((complementary or {}).get('peer_instance') or '')
        self.update_core_controls()
        self.frequency.set(str(s['analysis']['frequency_guess_hz'] or ''));self.offsets.set(','.join(map(str,s['analysis']['offsets_hz'])))
        self.update_source_controls()
        self.render(api.complete(s))

    def browse(self):
        self.status.set('TB Generator is reading the library catalog…')
        cds=self.cds.get();root=self.tbgen.get() or None;workspace=self.app.controller.workspace
        revision = self.revision
        self.app._submit('Testbench', lambda: api.browse_libraries(cds, workspace=workspace, tbgen_root=root),
                         lambda result: self.browsed(result) if revision == self.revision else None)

    def browsed(self,r):
        self.catalog={row['name']:row for row in r['catalog']['libraries']};self.boxes['Library']['values']=sorted(self.catalog);self.status.set('Select a library and cell, then read ports.')

    def library_changed(self,event=None):self.boxes['Cell']['values']=sorted(self.catalog[self.lib.get()]['cells'])
    def cell_changed(self,event=None):self.boxes['View']['values']=self.catalog[self.lib.get()]['cells'][self.cell.get()]

    def describe(self):
        lcv=dict(lib=self.lib.get(),cell=self.cell.get(),view=self.view.get());cds=self.cds.get();root=self.tbgen.get() or None
        self.invalidate();revision=self.revision
        workspace=self.app.controller.workspace
        self.app._submit('Testbench',lambda:api.describe_cell(lcv,cds_lib=cds,workspace=workspace,tbgen_root=root),lambda r:self.described(r) if revision==self.revision else None)

    def described(self,r):
        self.invalidate()
        tb=self.request.get('tbgen_spec',self.request)
        tb['ports']=[dict(name=p['name'],net=p['name'],role='unknown',direction=p['direction']) for p in r['descriptor']['ports']]
        self.suggestions={p['name']:p['suggested_role'] for p in r['classification']['ports']};self.show_ports(tb['ports'])
        self.status.set('Double-click a port to confirm its role and voltage or bias. Confirm LSB order, voltage domain, and decimal codes in the register table.')

    def show_ports(self,ports):
        self.table.delete(*self.table.get_children())
        for p in ports:self.table.insert('', 'end',iid=p['name'],text=p['name'],values=(p['role'],p['net'],''))
        names=[p['net'] for p in ports];self.boxes['Output P']['values']=names;self.boxes['Output N']['values']=names

    def edit_port(self,event=None):
        names=self.table.selection()
        if not names or names[0].startswith('plan:'):return
        name=names[0];tb=self.request.get('tbgen_spec',self.request);ports=tb.get('ports',[])
        port=next((p for p in ports if p['name']==name),None)
        if not port:return
        role=simpledialog.askstring('Confirm port',name+' role supply/ground/output_p/output_n/bias/control/register/unused',initialvalue=port.get('role') if port.get('role')!='unknown' else getattr(self,'suggestions',{}).get(name,'unknown'),parent=self)
        if role not in ('supply','ground','output_p','output_n','bias','control','register','unused'):return
        if role in ('supply','bias','control'):
            value=simpledialog.askfloat('Confirm drive',name+' voltage (V)',parent=self)
            if value is None:return
            category='supplies' if role=='supply' else 'controls';drivers=tb.setdefault(category,[])
            drivers[:]=[d for d in drivers if d['positive']!=port['net']]
            row=dict(name='SRC_'+name,positive=port['net'],negative='0',value=value)
            if category=='controls':row['kind']='voltage'
            drivers.append(row)
        port['role']=role
        if role=='ground':port['net']='0'
        if role=='output_p':self.p.set(port['net'])
        if role=='output_n':self.n.set(port['net'])
        self.invalidate();self.show_ports(ports)

    def add_register(self):
        win=tk.Toplevel(self);win.title('Decimal-to-binary Verilog-A')
        fields={}
        for row,(key,label,initial) in enumerate([('name','Register name','MAIN'),('members','Ports, LSB first, comma-separated',''),('variable','Code variable','MAIN_CODE'),('code','Decimal code','0'),('vdd','Logic-high supply node','VDD'),('vss','Logic-low reference node','0')]):
            ttk.Label(win,text=label).grid(row=row,column=0,sticky='w');fields[key]=tk.StringVar(value=initial);ttk.Entry(win,textvariable=fields[key],width=45).grid(row=row,column=1)
        def apply():
            try:
                members=[v.strip() for v in fields['members'].get().split(',') if v.strip()];code=int(fields['code'].get())
                if not 1<=len(members)<=30 or len(set(members))!=len(members) or not 0<=code<2**len(members):raise ValueError('Use 1–30 unique ports and a code within that bit width')
                tb=self.request.get('tbgen_spec',self.request);ports={p['name']:p for p in tb.get('ports',[])}
                if any(v not in ports for v in members):raise ValueError('Each bit must correspond to a discovered port')
                name=fields['name'].get();variable=fields['variable'].get()
                tb.setdefault('registers',[]).append(dict(name=name,width=len(members),members_lsb_first=members,code_variable=variable,vdd=fields['vdd'].get(),vss=fields['vss'].get(),implementation='veriloga',invert=0))
                tb.setdefault('variables',{})[variable]=code
                for member in members:ports[member]['role']='register'
                self.invalidate();self.show_ports(list(ports.values()));win.destroy()
            except Exception as exc:self.app._show_error(exc)
        ttk.Button(win,text='Confirm bit order, domain, and code',command=apply).grid(row=7,column=0,columnspan=2)

    def edit_request(self):
        win=tk.Toplevel(self);win.title('Drivers, registers, and models (TB Generator spec)');win.geometry('900x600')
        text=tk.Text(win,wrap='word');text.pack(fill='both',expand=True);text.insert('1.0',json.dumps(self.request,ensure_ascii=False,indent=2))
        def apply():
            try:self.request=json.loads(text.get('1.0','end'));self.invalidate();win.destroy()
            except Exception as exc:self.app._show_error(exc)
        ttk.Button(win,text='Apply',command=apply).pack()

    def select_gm(self, variable=None):
        variable = self.gm if variable is None else variable
        topology=self.request.get('topology_file')
        if not topology:
            generated = self.request.get('generated_dir') or self.request.get('tbgen_spec', {}).get('source', {}).get('generated_dir')
            if generated and (Path(generated) / 'tb.scs').is_file():
                topology = str(Path(generated) / 'tb.scs')
        if not topology and self.prepared:topology=self.prepared['spec']['extensions'].get('topology_file')
        if not topology:
            topology=filedialog.askopenfilename(parent=self,title='Select the generated DUT netlist to check the instance hierarchy')
            if not topology:return
            self.request['topology_file']=topology
        request=selection_request({'source':{'files':[{'path':topology}]}},provider=topology)
        names = [r['name'] for r in request['candidates'] if r['kind'] == 'device']
        self.boxes['Target gm']['values'] = names
        self.second_box['values'] = names
        win=tk.Toplevel(self);win.title('Select MOS occurrence / D G S B' if self.complementary.get() else 'Target gm hierarchy / D G S B');tree=ttk.Treeview(win);tree.pack(fill='both',expand=True)
        for row in request['candidates']:
            if row['kind']=='device':tree.insert('', 'end',iid=row['name'],text=row['name']+'  '+str(row['details']['terminals']))
        def choose(event=None):
            if tree.selection():variable.set(tree.selection()[0]);win.destroy()
        tree.bind('<Double-1>',choose);ttk.Button(win,text='Select' if self.complementary.get() else 'Select',command=choose).pack()

    def prepare(self):
        try:
            req=read(self.request);tb=req.get('tbgen_spec',req)
            tb['cds_lib']=self.cds.get()
            tb['dut']=dict(lib=self.lib.get(),cell=self.cell.get(),view=self.view.get())
            entry = self.entry.get()
            workspace = self.app.controller.workspace
            target = tb.setdefault('maestro',dict(lib='ISF_'+self.lib.get(),cell=self.cell.get()+'_ISF',view='maestro',test='HB_ISF'))
            if entry == 'design-lab' and self.source.get():
                req['generated_dir'] = self.source.get()
                if tb is not req:
                    tb.setdefault('source', {})['generated_dir'] = self.source.get()
                tb.setdefault('library_path', str(workspace / 'oa' / target['lib']))
            elif entry == 'maestro' and self.source.get():
                setup = Path(self.source.get()).resolve()
                if not self.source_test.get().strip():
                    raise ValueError('Enter the source test name in the selected Maestro setup.')
                tb['library_path'] = str(setup.parent.parent)
                target.update(lib=setup.parent.parent.name, cell=setup.parent.name, view=setup.name,
                              source_test=self.source_test.get().strip())
            overrides=dict(gm=dict(target_instance=self.gm.get() or None,peer_instances=[v.strip() for v in self.peer.get().split(',') if v.strip()]),output=dict(positive=self.p.get() or None,negative=self.n.get() or None),analysis=dict(offsets_hz=expand_offsets({'offsets': {'mode': 'list', 'values': self.offsets.get()}})))
            if self.complementary.get():
                overrides['gm']['complementary'] = dict(target_instance=self.second_gm.get().strip() or None,
                                                        peer_instance=self.second_peer.get().strip() or None)
                if self.core_mode.get() == 'asymmetric_pair':
                    overrides['gm']['complementary'].update(mode='asymmetric_pair', peer_instance=None)
                    overrides['gm']['peer_instances'] = []
            if self.frequency.get():overrides['analysis']['frequency_guess_hz']=float(self.frequency.get())
            fn={'design-lab':api.prepare_from_design_lab,'maestro':api.prepare_from_maestro,'cell':api.prepare_from_cell}[self.entry.get()]
            root=self.tbgen.get() or None;workspace=self.app.controller.workspace
            self.status.set('TB Generator is checking source and plan…')
            revision=self.revision
            self.app._submit('Testbench',lambda:fn(req,workspace=workspace,tbgen_root=root,overrides=overrides),lambda r:self.render(r) if revision==self.revision else None)
        except Exception as exc:self.app._show_error(exc)

    def render(self,r):
        self.prepared=r;self.table.delete(*self.table.get_children())
        self.review_button.configure(state='normal' if r.get('plan') else 'disabled')
        for i,p in enumerate(r['plan']):self.table.insert('','end',iid='plan:'+str(i),text=p['field'],values=(p['op'],'',p['reason']+(' '+json.dumps(p['value'],ensure_ascii=False) if 'value' in p else '')))
        bundle=r['spec']['extensions'].get('analysis_bundle',{})
        if bundle.get('shared'):
            group=self.table.insert('', 'end',text='HB / HBNOISE / HBXF analysis bundle',open=True)
            for key,value in bundle['shared'].items():
                self.table.insert(group,'end',text=key,values=('Shared','',json.dumps(value,ensure_ascii=False)))
            for output in r['spec']['measurements']['outputs']:
                self.table.insert(group,'end',text=output['name'],values=(output['kind'],output['units'],output['source']))
        proof=' ·'.join(k+'='+v['resolved_net'] for k,v in r['spec']['gm']['terminals'].items())
        self.status.set(('Ready. Awaiting simulation. '+proof) if r['status']=='ready' else 'Confirmation required. '+' ·'.join(i['message'] for i in r['issues']))
        if r['status']=='needs_input' and r['spec']['source']['kind']=='maestro' and all(i['field']=='/execution/materialization' for i in r['issues']):
            tb=r['spec']['extensions']['tbgen_spec'];m=tb['maestro']
            self.review_settings(r)
            if messagebox.askyesno('Confirm write to source Maestro',r['spec']['source']['maestro_dir']+'\nCopy '+m['source_test']+' → '+m['test']+'\nAdd HB, HBNOISE, HBXF, and saves. Keep existing tests, variables, corners, and sweeps.\nBack up before writing and verify the saved setup. Continue?',parent=self):
                self.render(api.complete(r['spec'],confirmations=[dict(field='/execution/materialization',value_hash=digest('source_new_test'))]));return
        self.start_button.configure(state='normal' if r['status']=='ready' else 'disabled')
        self.session_button.configure(state='normal' if r['status']=='ready' and self.session_context(r) else 'disabled')

    def session_context(self,r):
        context=getattr(self.app,'maestro_context',{})
        if r['spec']['source']['kind']=='maestro' and context.get('session_add') and context.get('maestro_dir')==r['spec']['source']['maestro_dir']:
            return context
        return None

    def review_settings(self,r):
        win=tk.Toplevel(self);win.title('New test · inherited settings and overrides');win.geometry('900x650')
        win.columnconfigure(0,weight=1);win.rowconfigure(2,weight=1)
        bundle=r['spec']['extensions'].get('analysis_bundle',{})
        shared=ttk.Treeview(win,columns=('value',),height=6);shared.heading('#0',text='HB / HBNOISE / HBXF shared parameters');shared.heading('value',text='Value');shared.grid(row=0,column=0,sticky='nsew')
        for key,value in bundle.get('shared',{}).items():shared.insert('','end',text=key,values=(json.dumps(value,ensure_ascii=False),))
        specific=ttk.Treeview(win,columns=('hb','hbnoise','hbxf'),height=6);specific.heading('#0',text='Analysis-specific parameters')
        options={a['kind']:a['parameters'] for a in bundle.get('analyses',[])}
        for kind in ('hb','hbnoise','hbxf'):specific.heading(kind,text=kind.upper());specific.column(kind,width=200)
        for key in ('oscic','oscmethod','tstab','noiseout','noisetype','freqaxis'):
            specific.insert('','end',text=key,values=tuple(json.dumps(options.get(k,{}).get(key,''),ensure_ascii=False) for k in ('hb','hbnoise','hbxf')))
        specific.grid(row=1,column=0,sticky='ew')
        text=tk.Text(win,wrap='word');text.grid(row=2,column=0,sticky='nsew')
        vertical=ttk.Scrollbar(win,orient='vertical',command=text.yview);vertical.grid(row=2,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(win,orient='horizontal',command=text.xview);horizontal.grid(row=3,column=0,sticky='ew')
        text.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        review=read(r['spec']['extensions'].get('new_test_review',dict(analysis_bundle=bundle,outputs=r['spec'].get('measurements',{}).get('outputs',[]))))
        saved=review.get('inherited',{}).pop('saved_setup_xml',{})
        text.insert('1.0', '\n'.join(f"{item['field']} · {item['op']}\n{item['reason']}\n"
                                     for item in r.get('plan', [])))
        text.insert('end',json.dumps(review,ensure_ascii=False,indent=2))
        for name,xml in saved.items():text.insert('end','\n\nSaved source '+name+'\n'+xml)
        text.configure(state='disabled')
        ttk.Button(win,text='Close review',command=win.destroy).grid(row=4,column=0,columnspan=2)
        win.transient(self.winfo_toplevel());win.grab_set();win.wait_window()

    def start_session(self):
        r=self.prepared
        if not r or r['status']!='ready':return
        context=self.session_context(r)
        if not context:
            self.app._show_error(ValueError('Reopen ISF Explorer from the menu in the intended Maestro window.'));return
        policy='reject'
        if context['session_add']['dirty']:
            if not messagebox.askyesno('Save Maestro settings','This Maestro session has unsaved changes. Save them and add the test?',parent=self):return
            policy='save'
        self.review_settings(r)
        self.start_button.configure(state='disabled');self.session_button.configure(state='disabled')
        self.status.set('Waiting for confirmation in Maestro, followed by readback verification…')
        self.session_prepared=r
        self.app._submit('Maestro transaction',lambda:api.submit_session_add(api.prepare_session_add(r['spec_path'],spec_hash=r['spec_hash'],context=context,save_policy=policy)),self.session_submitted)

    def session_submitted(self,handle):
        self.session_handle=handle;self.poll_session()

    def poll_session(self):
        if not self.winfo_exists():return
        self._poll=None
        def received(state):
            if not self.winfo_exists():return
            if state['status']=='awaiting_session_confirmation':self._poll=self.after(500,self.poll_session)
            elif state['status']=='added':
                r=self.session_prepared
                self.status.set('Readback passed. Preparing a private execution copy…')
                workspace=self.app.controller.workspace
                def run():
                    api.accept_session_add(self.session_handle)
                    return api.run(r['spec_path'],spec_hash=r['spec_hash'],workspace=workspace,request_id='session-'+r['spec_hash'])
                self.app._submit('Maestro transaction',run,self.started)
            else:
                self.status.set('Maestro transaction status '+state['status']+'. Analysis did not start.')
        self.app._submit('Maestro transaction',lambda:api.session_add_status(self.session_handle),received)

    def start(self):
        r=self.prepared
        if not r or r['status']!='ready':return
        self.start_button.configure(state='disabled');workspace=self.app.controller.workspace
        self.app._submit('Testbench',lambda:api.run(r['spec_path'],spec_hash=r['spec_hash'],workspace=workspace,request_id='ui-'+r['spec_hash']),self.started)

    def started(self,job):self.job=job;self.poll()
    def poll(self):
        if not self.winfo_exists():return
        manifest=read(self.job['manifest_path']);state=manifest['job']['state'];stage=manifest['stage']
        self.status.set(('TB Generator' if stage.startswith('tbgen') else 'ISF')+' · '+stage+' · '+state)
        if state in ('queued','running'):self._poll=self.after(1200,self.poll)
        elif state in ('completed','partial'):
            self.app.status_var.set('ISF analysis complete · flicker + thermal' if state=='completed' else 'Partial ISF results available. Check the incomplete mechanisms.')
            job_id = manifest['job']['job_id']
            self.app.current_job_id = job_id
            self.app._refresh_jobs(silent=True)
            self.app._schedule_poll(0)
        else:
            errors=' ·'.join(i['message'] for i in manifest['issues']);self.status.set('Failed. '+errors[:800])
            if 'SETUP_OPEN_OR_LOCKED' in errors:
                self.session_button.configure(state='normal' if self.prepared and self.session_context(self.prepared) else 'disabled')
                messagebox.showinfo('Maestro opened','Open ISF Explorer from this Maestro session and choose “Add test in current Maestro and analyze”. Confirm the transaction in Maestro to run in a private copy.',parent=self)
