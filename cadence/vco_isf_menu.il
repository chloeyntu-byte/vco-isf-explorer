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
  let((setup selected history lib cell view viewObj port entry name variable)
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
    close(port)
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
