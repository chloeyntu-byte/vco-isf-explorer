; Portable Maestro design-variable migration for IC6.1.8.
; Load definitions only. Call these functions after backing up the exact views.
; Never invoke on an editable view owned by another Virtuoso session.
; After saving, close/reopen and verify unique native variables and a fresh
; named history. IC618 can retain a stale variable-table row after shortening
; an existing active.state list; an in-memory result is not persistence proof.

procedure(ISFRequireAssemblerSetup(isfSession)
 let((isfView isfBag isfSubtype)
  isfView=ddGetObj(axlGetSessionLibName(isfSession)
                  axlGetSessionCellName(isfSession)
                  axlGetSessionViewName(isfSession))
  isfBag=dbOpenBag(isfView "r")
  when(isfBag
   isfSubtype=isfBag~>viewSubType
   dbCloseBag(isfBag))
  when(equal(isfSubtype "explorer")
   error("ISF global migration requires ADE Assembler. Back up and explicitly convert this Explorer view before migration; otherwise its global sweep may run only one point."))
  t))

procedure(ISFPromoteTestVariables(isfSession isfTests isfNames @optional isfKeepOverrides)
 let((isfDB isfDefaults isfGroups isfTest isfName isfHandle isfValue
       isfDefault isfGlobal isfSetName isfSet isfMembers isfNewSet isfPair)
  ISFRequireAssemblerSetup(isfSession)
  isfDB=axlGetMainSetupDB(isfSession)
  isfDefaults=nil
  isfGroups=nil
  ; Validate every value and complete parametric group before changing anything.
  foreach(isfName isfNames
   isfDefault=nil
   foreach(isfTest isfTests
    isfHandle=axlGetVar(axlGetTest(isfDB isfTest) isfName)
    when(and(isfHandle isfHandle!=0)
     isfValue=axlGetVarValue(isfHandle)
     when(and(isfValue isfValue!="")
     if(isfDefault then
      unless(equal(isfValue isfDefault)
       unless(member(list(isfTest isfName isfValue) isfKeepOverrides)
        error("ISF global conflict: test=%s variable=%s value=%L default=%L" isfTest isfName isfValue isfDefault)))
     else isfDefault=isfValue))))
   isfGlobal=axlGetVar(isfDB isfName)
   when(and(isfGlobal isfGlobal!=0)
    isfValue=axlGetVarValue(isfGlobal)
    if(isfDefault then
     unless(equal(isfValue isfDefault) error("ISF existing global differs: %s" isfName))
    else isfDefault=isfValue))
   unless(isfDefault error("ISF requested variable missing: %s" isfName))
   isfDefaults=append(isfDefaults list(list(isfName isfDefault))))
  foreach(isfTest isfTests
   foreach(isfSetName cadr(_axlGetVariableSets(axlGetTest(isfDB isfTest)))
    isfSet=_axlGetVariableSet(axlGetTest(isfDB isfTest) isfSetName)
    isfMembers=cadr(axlGetVars(isfSet))
    when(setof(isfName isfMembers member(isfName isfNames))
     unless(length(setof(isfName isfMembers member(isfName isfNames)))==length(isfMembers)
      error("ISF refuses partial parametric-group migration: test=%s group=%s" isfTest isfSetName))
     when(_axlGetVariableSet(isfDB isfSetName)!=0
      error("ISF global parametric-group name collision: %s" isfSetName))
     foreach(isfName isfMembers
      when(setof(isfPair isfKeepOverrides equal(car(isfPair) isfTest)&&equal(cadr(isfPair) isfName))
       error("ISF cannot migrate grouped test override: %s/%s" isfTest isfName)))
     isfGroups=append(isfGroups list(list(isfTest isfSetName isfMembers))))))
  foreach(isfPair isfDefaults
   unless(maeSetVar(car(isfPair) cadr(isfPair) ?session isfSession)
    error("ISF failed to set global: %s" car(isfPair))))
  ; A retained test override must exclude the same-name global from that test.
  ; Otherwise a fresh batch netlist can declare both values (Spectre SFE-59).
  foreach(isfPair isfKeepOverrides
   _axlSetEnabledGlobalVarPerTest(isfDB cadr(isfPair) car(isfPair) nil)
   when(axlGetEnabledGlobalVarPerTest(isfDB cadr(isfPair) car(isfPair))
    error("ISF failed to preserve test-local override scope: %s/%s" car(isfPair) cadr(isfPair))))
  foreach(isfPair isfGroups
   isfNewSet=_axlPutVariableSet(isfDB cadr(isfPair))
   when(or(!isfNewSet isfNewSet==0) error("ISF cannot create global parametric group"))
   foreach(isfName caddr(isfPair) axlPutVar(isfNewSet isfName ""))
   axlRemoveElement(_axlGetVariableSet(axlGetTest(isfDB car(isfPair)) cadr(isfPair))))
  foreach(isfTest isfTests
   foreach(isfName isfNames
    isfHandle=axlGetVar(axlGetTest(isfDB isfTest) isfName)
    when(and(isfHandle isfHandle!=0)
     isfValue=axlGetVarValue(isfHandle)
     unless(member(list(isfTest isfName isfValue) isfKeepOverrides)
      axlRemoveElement(isfHandle)))))
  isfDefaults))

procedure(ISFPortablePwlDefaults(isfSession isfLib isfCell isfBindings @optional isfTests)
 let((isfCV isfEntry isfInstance isfFile isfValue)
  ; bindings: ((source-instance global-variable cell-relative-waveform-file) ...)
  ; The caller stages the files alongside the cell before invoking this function.
  foreach(isfEntry isfBindings
   isfFile=strcat(ddGetObj(isfLib isfCell)~>readPath "/" caddr(isfEntry))
   unless(isFile(isfFile) error("ISF cell-relative waveform missing: %s" isfFile)))
  isfCV=dbOpenCellViewByType(isfLib isfCell "schematic" "" "a")
  unless(isfCV error("ISF could not open source schematic"))
  foreach(isfEntry isfBindings
   isfInstance=car(setof(x isfCV~>instances x~>name==car(isfEntry)))
   unless(isfInstance error("ISF source instance missing: %s" car(isfEntry)))
   unless(member(isfInstance~>cellName '("vpwlf" "vsource")) error("ISF expected PWL voltage source")))
  foreach(isfEntry isfBindings
   isfInstance=car(setof(x isfCV~>instances x~>name==car(isfEntry)))
   dbReplaceProp(isfInstance "fileName" "string" sprintf(nil "pPar(\"%s\")" cadr(isfEntry)))
   isfValue=sprintf(nil "strcat(ddGetObj(\"%s\" \"%s\")~>readPath \"/%s\")" isfLib isfCell caddr(isfEntry))
   unless(maeSetVar(cadr(isfEntry) isfValue ?session isfSession) error("ISF cannot set PWL global"))
   ; pPar file references may not be discovered as test design-variable uses.
   foreach(isfTest isfTests
    _axlSetEnabledGlobalVarPerTest(axlGetMainSetupDB(isfSession) cadr(isfEntry) isfTest t)))
  schCheck(isfCV)
  dbSave(isfCV)
  dbClose(isfCV)
  t))
