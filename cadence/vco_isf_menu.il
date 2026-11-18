; Session bridge registry is per Virtuoso process and per invoking window.
unless(boundp('isfMaestroSessionBridges) isfMaestroSessionBridges=makeTable('isfMaestroSessionBridges nil))

procedure(isfMaestroConfirmSessionAdd(review)
  hiDisplayAppDBox(?name 'isfSessionConfirm ?dboxBanner "Add ISF test"
    ?dboxText strcat("Review all inherited and added settings in:\n" review "\n\nSave the current setup if requested, add the ISF test, verify preservation, and continue in a private execution copy?")
    ?dialogType hicQuestionDialog ?buttonLayout 'YesNo)
)

procedure(isfMaestroRegisterBridge(session window bridge generation)
  isfMaestroSessionBridges[bridge]=list(session window generation nil)
  unless(boundp('isfMaestroPrivateBatch) && isfMaestroPrivateBatch
    hiRegTimer(sprintf(nil "isfMaestroPollSessionAdd(%L)" bridge) 10))
)

procedure(isfMaestroSessionStatus(bridge status)
  let((o)
    o=outfile(strcat(bridge "/native-status.txt"))
    unless(o error("Cannot write ISF session status"))
    fprintf(o "%s\n" status) close(o)
  )
)

procedure(isfMaestroPollSessionAdd(bridge)
  let((entry result)
    entry=isfMaestroSessionBridges[bridge]
    when(entry && !cadddr(entry)
      if(!maeIsValidMaestroSession(car(entry)) then
        isfMaestroSessionBridges[bridge]=nil
      else
        if(isFile(strcat(bridge "/request.il")) then
          isfMaestroSessionBridges[bridge]=list(car(entry) cadr(entry) caddr(entry) t)
          result=errset(load(strcat(bridge "/request.il")) t)
          unless(result isfMaestroSessionStatus(bridge "failed"))
        else
          unless(boundp('isfMaestroPrivateBatch) && isfMaestroPrivateBatch
            hiRegTimer(sprintf(nil "isfMaestroPollSessionAdd(%L)" bridge) 10))
        )
      )
    )
  )
)

procedure(isfMaestroApplySessionAdd(bridge session generation script receipt review)
  let((entry result)
    entry=isfMaestroSessionBridges[bridge]
    unless(entry && equal(car(entry) session) && equal(caddr(entry) generation)
      error("ISF session bridge identity mismatch"))
    unless(maeIsValidMaestroSession(session) error("ISF caller session closed"))
    when(cadr(entry)
      unless(equal(axlGetWindowSession(cadr(entry)) session) error("ISF window changed")))
    if(isFile(strcat(bridge "/cancelled")) then
      isfMaestroSessionStatus(bridge "cancelled")
    else
      if(isfMaestroConfirmSessionAdd(review) then
        ; The dependency-generated transaction rechecks LCV, setup, library,
        ; dirty state and revision, performs native rollback and writes receipt.
        when(isFile(strcat(bridge "/cancelled")) error("ISF transaction expired while awaiting confirmation"))
        result=errset(load(script) t)
        isfMaestroSessionStatus(bridge if(result && isFile(receipt) then "executed" else "failed"))
      else
        isfMaestroSessionStatus(bridge "rejected")
      )
    )
  )
)

; VCO ISF: independent ADE menu. Only the launcher path needs relocating.
; Context is captured at click time from the invoking ADE window.
; Point the menu at your checkout either by exporting VCO_ISF_HOME (the
; directory that contains cadence/ and run.py) or by setting isfMaestroLauncher
; in your .cdsinit before loading this file.
unless(boundp('isfMaestroLauncher)
  isfMaestroLauncher = if(getShellEnvVar("VCO_ISF_HOME")
    strcat(getShellEnvVar("VCO_ISF_HOME") "/cadence/vco_isf_launch")
    "/path/to/vco-isf-explorer/cadence/vco_isf_launch")
)

procedure(isfMaestroWriteContext(session context)
  let((setup selected history lib cell view viewObj port entry name variable window bridge generation)
    setup = axlGetMainSetupDB(session)
    lib = axlGetSessionLibName(session)
    cell = axlGetSessionCellName(session)
    view = axlGetSessionViewName(session)
    viewObj = ddGetObj(lib cell view)
    unless(viewObj error("Cannot locate the source ADE view.\n"))
    history = axlGetCurrentHistory(session)
    selected = nil
    when(history && history != 0 selected = axlGetHistoryName(history))
    port = outfile(context)
    unless(port error("Cannot create the VCO ISF source context.\n"))
    fprintf(port "ISF_MAESTRO_CONTEXT_1\n")
    fprintf(port "C\t%L\t%L\t%L\t%L\t%L\t%L\t%L\n"
      lib cell view getWorkingDir() viewObj->readPath selected
      prependInstallPath("bin/ocean"))
    fprintf(port "R\t%L\n" getShellEnvVar("LD_LIBRARY_PATH"))
    foreach(variable '("PATH" "CDS_LIC_FILE" "LM_LICENSE_FILE")
      fprintf(port "E\t%L\t%L\n" variable getShellEnvVar(variable))
    )
    foreach(name cadr(axlGetHistory(setup))
      entry = axlGetHistoryEntry(setup name)
      when(entry
        fprintf(port "H\t%L\t%L\t%L\n" name
          axlGetHistoryResultsDir(entry) axlGetHistoryResults(entry))
      )
    )
    window=hiGetCurrentWindow()
    bridge=strcat(context ".bridge")
    generation=context
    fprintf(port "S\t%L\t%L\t%L\t%L\t%L\t%L\n"
      session if(window then sprintf(nil "%d" window->windowNum) else nil)
      sprintf(nil "%d" setup) if(maeIsSetupModified(?session session) then "true" else "false")
      generation bridge)
    close(port)
    isfMaestroRegisterBridge(session if(window then window->windowNum else nil) bridge generation)
    context
  )
)

procedure(isfMaestroOpenCurrent()
  let((session context)
    session = axlGetWindowSession()
    unless(session error("Open VCO ISF from the source ADE window.\n"))
    context = strcat(getTempDir() "/" makeTempFileName("vco_isf_XXXXXX"))
    isfMaestroWriteContext(session context)
    ; Quote both arguments; each call starts an independent context-bound UI.
    ipcBeginProcess(sprintf(nil "%L %L" isfMaestroLauncher context))
    printf("VCO ISF opened for %s/%s/%s\n"
      axlGetSessionLibName(session) axlGetSessionCellName(session)
      axlGetSessionViewName(session))
  )
)

procedure(isfMaestroMenuItemFactory()
  hiCreateMenuItem(
    ?name 'isfMaestroOpenItem ?itemText "Open VCO ISF Explorer"
    ?callback "isfMaestroOpenCurrent()"
  )
)

; Register only our item; preserve every other plugin factory on reload.
unless(boundp('maestroPluginItemFactories) maestroPluginItemFactories = nil)
unless(member('isfMaestroMenuItemFactory maestroPluginItemFactories)
  maestroPluginItemFactories = append(maestroPluginItemFactories list('isfMaestroMenuItemFactory))
)

procedure(isfMaestroMenuTrigger(_args)
  ; The existing Plug-in menu owns this item when its extension is available.
  ; A standalone installation retains an independent VCO ISF fallback menu.
  if(boundp('maestroPluginMenuExtensionVersion) && maestroPluginMenuExtensionVersion >= 1 then
    nil
  else
    list(hiCreatePulldownMenu(
      'isfMaestroMenu "VCO ISF" list(isfMaestroMenuItemFactory())
    ))
  )
)
; A reload updates callbacks without adding another copy of this menu.
unless(boundp('isfMaestroMenusRegistered) && isfMaestroMenusRegistered
  deRegUserTriggers("explorer" nil 'isfMaestroMenuTrigger)
  deRegUserTriggers("maestro" nil 'isfMaestroMenuTrigger)
  deRegUserTriggers("adexl" nil 'isfMaestroMenuTrigger)
  isfMaestroMenusRegistered = t
)
