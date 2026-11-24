package com.example.pluginhost

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import org.json.JSONObject

@Composable
internal fun WorkshopChat(onOpenPlugins: () -> Unit) {
    val session = rememberDesktopSession()
    var showPairing by rememberSaveable { mutableStateOf(false) }
    var showNewProject by rememberSaveable { mutableStateOf(false) }
    var draft by rememberSaveable { mutableStateOf("") }
    var draftTitle by rememberSaveable { mutableStateOf<String?>(null) }
    val drawer = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val focus = LocalFocusManager.current
    val project = session.project
    var threadDraft by rememberSaveable(project?.id) { mutableStateOf("") }
    val colors = MaterialTheme.colorScheme
    val openPairing = {
        focus.clearFocus()
        showPairing = true
    }
    val goBack: () -> Unit = {
        when {
            drawer.isOpen -> scope.launch { drawer.close() }
            showPairing -> showPairing = false
            project != null -> session.closeProject()
        }
    }
    BackHandler(enabled = drawer.isOpen || showPairing || project != null, onBack = goBack)
    LaunchedEffect(session) { session.start() }
    ModalNavigationDrawer(
        drawerState = drawer,
        gesturesEnabled = !showPairing,
        drawerContent = {
            WorkshopHistory(
                session = session,
                onDismiss = { scope.launch { drawer.close() } },
                onNew = {
                    scope.launch { drawer.close() }
                    if (session.link == null) openPairing() else showNewProject = true
                },
                onPlugins = {
                    scope.launch {
                        drawer.close()
                        onOpenPlugins()
                    }
                },
                onProject = {
                    focus.clearFocus()
                    session.openProject(it)
                    scope.launch { drawer.close() }
                },
            )
        },
    ) {
        Column(Modifier.fillMaxSize().background(colors.background).safeDrawingPadding().imePadding()) {
            WorkshopTopBar(
                title = when {
                    showPairing -> "连接设置"
                    project != null -> project.name
                    else -> "插件工坊"
                },
                icon = if (showPairing || project != null) WorkshopGlyph.Back else WorkshopGlyph.Menu,
                label = if (showPairing || project != null) "返回会话" else "打开会话菜单",
                onClick = {
                    if (showPairing || project != null) goBack() else {
                        focus.clearFocus()
                        scope.launch { drawer.open() }
                    }
                },
                action = { if (!showPairing) ConnectionBadge(session, openPairing) },
            )
            Box(
                Modifier.fillMaxWidth().weight(1f),
                contentAlignment = Alignment.TopCenter,
            ) {
                when {
                    showPairing -> DesktopPairingForm(session, Modifier.widthIn(max = 600.dp))
                    project != null -> key(project.id) {
                        WorkshopThread(
                            session, project, threadDraft,
                            onBodyChange = { threadDraft = it },
                            onSubmitted = { if (threadDraft == it) threadDraft = "" },
                        )
                    }
                    else -> Column(Modifier.widthIn(max = 680.dp).fillMaxSize()) {
                        Column(
                            Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState())
                                .padding(start = 22.dp, end = 22.dp, top = 12.dp, bottom = 18.dp),
                        ) {
                            WorkshopWelcome(
                                onIdea = {
                                    draft = it.prompt
                                    draftTitle = it.title
                                },
                            )
                            connectionProblem(session)?.let { WorkshopNotice(it) }
                            session.notice?.let { WorkshopNotice(it) }
                        }
                        WorkshopComposer(
                            body = draft,
                            onBodyChange = {
                                draft = it
                                draftTitle = null
                            },
                            enabled = !session.busy,
                            onSend = {
                                focus.clearFocus()
                                if (session.link == null) {
                                    openPairing()
                                } else {
                                    val submitted = draft
                                    session.createProject(draftTitle ?: projectName(submitted), submitted) {
                                        if (draft == submitted) {
                                            draft = ""
                                            draftTitle = null
                                        }
                                    }
                                }
                            },
                        )
                    }
                }
            }
        }
    }
    if (showNewProject) {
        NewProjectDialog(session, onDismiss = { showNewProject = false })
    }
}

@Composable
private fun rememberDesktopSession(): DesktopConversationSession {
    val context = LocalContext.current
    return remember(context) { DesktopConversationSession.forApp(context) }
}

private fun projectName(body: String): String {
    val firstLine = body.trim().lineSequence().first()
    val count = firstLine.codePointCount(0, firstLine.length)
    return if (count > 24) firstLine.substring(0, firstLine.offsetByCodePoints(0, 24)) + "…" else firstLine
}

private fun connectionProblem(session: DesktopConversationSession): String? {
    val link = session.link ?: return null
    val status = link.blockedReason ?: session.connection
    return status?.takeUnless { it == "CONNECTED" }?.let(::desktopFailureText)
}

@Composable
private fun WorkshopHistory(
    session: DesktopConversationSession,
    onDismiss: () -> Unit,
    onNew: () -> Unit,
    onPlugins: () -> Unit,
    onProject: (DesktopProject) -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    ModalDrawerSheet(
        modifier = Modifier.widthIn(max = 330.dp).fillMaxWidth(0.88f),
        drawerContainerColor = colors.background,
        windowInsets = WindowInsets(0, 0, 0, 0),
    ) {
        Column(Modifier.fillMaxSize().safeDrawingPadding().padding(horizontal = 18.dp, vertical = 12.dp)) {
            Row(
                Modifier.fillMaxWidth().padding(bottom = 18.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                WorkshopIconButton(WorkshopGlyph.Back, "关闭会话菜单", onDismiss)
                Text("插件工坊", style = MaterialTheme.typography.titleLarge)
            }
            Surface(
                onClick = onNew,
                enabled = !session.busy,
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(16.dp),
                color = colors.primaryContainer,
                contentColor = colors.onPrimaryContainer,
            ) {
                DrawerLabel(WorkshopGlyph.Plus, "新建项目", colors.onPrimaryContainer)
            }
            Spacer(Modifier.height(8.dp))
            Surface(onClick = onPlugins, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(16.dp)) {
                DrawerLabel(WorkshopGlyph.Plugins, "我的插件", colors.onSurface)
            }
            HorizontalDivider(Modifier.padding(vertical = 22.dp), color = colors.outlineVariant)
            Text("会话历史", Modifier.padding(horizontal = 10.dp), style = MaterialTheme.typography.titleMedium)
            if (session.projects.isEmpty()) {
                Text(
                    "发送第一条需求后，会话会保存在这里。",
                    Modifier.padding(horizontal = 10.dp, vertical = 12.dp),
                    style = MaterialTheme.typography.bodyMedium,
                    color = colors.onSurfaceVariant,
                )
            }
            LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(top = 10.dp)) {
                items(session.projects, key = { it.id }) { item ->
                    val selected = item.id == session.project?.id
                    Surface(
                        onClick = { onProject(item) },
                        enabled = !session.busy,
                        modifier = Modifier.fillMaxWidth(),
                        color = if (selected) colors.primaryContainer else Color.Transparent,
                        shape = RoundedCornerShape(12.dp),
                    ) {
                        Column(Modifier.padding(horizontal = 12.dp, vertical = 12.dp)) {
                            Text(item.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            if (item.fingerprint != session.link?.endpoint?.fingerprint) {
                                Text("关联另一台电脑", style = MaterialTheme.typography.labelMedium, color = colors.onSurfaceVariant)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun DrawerLabel(icon: WorkshopGlyph, label: String, tint: Color) {
    Row(
        Modifier.padding(horizontal = 14.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        WorkshopIcon(icon, tint)
        Text(label, style = MaterialTheme.typography.titleMedium)
    }
}

@Composable
private fun ConnectionBadge(session: DesktopConversationSession, onClick: () -> Unit) {
    val colors = MaterialTheme.colorScheme
    val connected = session.link?.blockedReason == null && session.connection == "CONNECTED"
    val green = if (isSystemInDarkTheme()) Color(0xFF8CDEB5) else Color(0xFF217651)
    val label = when {
        connected -> "电脑已连接"
        session.link == null -> "连接电脑"
        else -> "连接设置"
    }
    Surface(
        onClick = onClick,
        shape = CircleShape,
        color = colors.surface,
        border = BorderStroke(1.dp, colors.outlineVariant),
    ) {
        Row(
            Modifier.heightIn(min = 48.dp).padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            if (connected) Box(Modifier.size(7.dp).background(green, CircleShape))
            else WorkshopIcon(WorkshopGlyph.Computer, colors.primary, Modifier.size(16.dp))
            Text(label, style = MaterialTheme.typography.labelMedium, color = if (connected) green else colors.onSurface)
        }
    }
}

@Composable
private fun NewProjectDialog(session: DesktopConversationSession, onDismiss: () -> Unit) {
    var name by rememberSaveable { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("新建项目") },
        text = {
            OutlinedTextField(
                value = name,
                onValueChange = { if (it.length <= 64) name = it },
                label = { Text("项目名称") },
                singleLine = true,
            )
        },
        confirmButton = {
            Button(enabled = name.isNotBlank() && !session.busy, onClick = {
                session.createProject(name, onSaved = onDismiss)
            }) { Text("创建") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

private data class WorkshopIdea(
    val title: String,
    val description: String,
    val prompt: String,
    val glyph: WorkshopGlyph,
    val light: Color,
    val dark: Color,
)

private val WorkshopIdeas = listOf(
    WorkshopIdea(
        "随手记", "记下灵感、清单和临时笔记",
        "我想做一个随手记插件，可以新增、编辑和删除笔记，把内容保存在本机。",
        WorkshopGlyph.Notes, Color(0xFFEDE6FF), Color(0xFF3C2C61),
    ),
    WorkshopIdea(
        "习惯打卡", "记录每天完成的小目标",
        "我想做一个习惯打卡插件，可以添加习惯、记录每天的完成情况，并查看本周记录。",
        WorkshopGlyph.Check, Color(0xFFDFF4EC), Color(0xFF233F3C),
    ),
    WorkshopIdea(
        "日常记账", "分类记录支出，查看小计",
        "我想做一个日常记账插件，可以填写金额和分类，把支出保存在本机，并查看本月分类小计。",
        WorkshopGlyph.Wallet, Color(0xFFFBECDD), Color(0xFF443229),
    ),
)

@Composable
private fun WorkshopWelcome(onIdea: (WorkshopIdea) -> Unit) {
    val colors = MaterialTheme.colorScheme
    val dark = isSystemInDarkTheme()
    val shape = RoundedCornerShape(28.dp)
    val gradient = if (dark) listOf(Color(0xFF30234D), Color(0xFF322B62))
        else listOf(Color(0xFFF0E8FF), Color(0xFFE3DEFF))
    BoxWithConstraints(
        Modifier.fillMaxWidth().clip(shape).background(Brush.linearGradient(gradient)),
    ) {
        val showArt = maxWidth.value / LocalDensity.current.fontScale >= 300f
        if (showArt) {
            Image(
                painterResource(R.drawable.workshop_spark),
                contentDescription = null,
                modifier = Modifier.align(Alignment.CenterEnd).fillMaxWidth(0.44f).padding(end = 8.dp),
            )
        }
        Column(
            Modifier.fillMaxWidth(if (showArt) 0.66f else 1f)
                .padding(start = 20.dp, end = 16.dp, top = 29.dp, bottom = 28.dp),
            verticalArrangement = Arrangement.spacedBy(9.dp),
        ) {
            Column {
                Text(
                    "把想法做成",
                    fontSize = 31.sp,
                    lineHeight = 37.sp,
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    "手机插件",
                    color = if (dark) Color(0xFFD0C0FF) else Color(0xFF5336CF),
                    fontSize = 34.sp,
                    lineHeight = 41.sp,
                    fontWeight = FontWeight.Bold,
                )
            }
            Text(
                "说说你想做什么，\n从一个想法开始。",
                style = MaterialTheme.typography.bodyMedium,
                color = if (dark) Color(0xFFE2D6FB) else Color(0xFF5F527B),
            )
        }
    }
    Text(
        "试试这些灵感",
        Modifier.padding(top = 25.dp, bottom = 11.dp),
        style = MaterialTheme.typography.titleMedium,
    )
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        WorkshopIdeas.forEach { idea ->
            Surface(
                onClick = { onIdea(idea) },
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(18.dp),
                color = colors.surface,
                border = BorderStroke(1.dp, colors.outlineVariant),
            ) {
                Row(
                    Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    Box(
                        Modifier.size(42.dp).background(if (dark) idea.dark else idea.light, RoundedCornerShape(13.dp)),
                        contentAlignment = Alignment.Center,
                    ) {
                        WorkshopIcon(idea.glyph, colors.onSurface)
                    }
                    Column(Modifier.weight(1f)) {
                        Text(idea.title, style = MaterialTheme.typography.titleMedium)
                        Text(idea.description, style = MaterialTheme.typography.bodySmall, color = colors.onSurfaceVariant)
                    }
                    WorkshopIcon(WorkshopGlyph.ArrowRight, colors.primary, Modifier.size(18.dp))
                }
            }
        }
    }
}

@Composable
private fun WorkshopComposer(body: String, onBodyChange: (String) -> Unit, enabled: Boolean, onSend: () -> Unit) {
    val colors = MaterialTheme.colorScheme
    val style = MaterialTheme.typography.bodyLarge
    Surface(
        modifier = Modifier.fillMaxWidth().padding(start = 18.dp, end = 18.dp, top = 6.dp, bottom = 12.dp),
        shape = RoundedCornerShape(23.dp),
        color = colors.surface,
        border = BorderStroke(1.dp, colors.outlineVariant),
        shadowElevation = 1.dp,
    ) {
        Row(
            Modifier.padding(start = 16.dp, top = 8.dp, end = 8.dp, bottom = 8.dp),
            verticalAlignment = Alignment.Bottom,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            BasicTextField(
                value = body,
                onValueChange = {
                    if (it.codePointCount(0, it.length) <= MessageProtocol.MAX_BODY_CHARS) onBodyChange(it)
                },
                modifier = Modifier.weight(1f).padding(vertical = 13.dp).semantics {
                    contentDescription = "输入插件需求"
                },
                textStyle = style.copy(color = colors.onSurface),
                cursorBrush = SolidColor(colors.primary),
                maxLines = 4,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences),
                decorationBox = { textField ->
                    Box {
                        if (body.isEmpty()) Text("描述你想做的插件…", style = style, color = colors.onSurfaceVariant)
                        textField()
                    }
                },
            )
            WorkshopIconButton(WorkshopGlyph.Send, "发送", onSend, enabled = enabled && body.isNotBlank(), filled = true)
        }
    }
}

@Composable
private fun WorkshopThread(
    session: DesktopConversationSession,
    project: DesktopProject,
    body: String,
    onBodyChange: (String) -> Unit,
    onSubmitted: (String) -> Unit,
) {
    val focus = LocalFocusManager.current
    val messages = session.messages
    val scroll = rememberLazyListState()
    val newest = messages.lastOrNull()?.id
    LaunchedEffect(newest) { if (newest != null) scroll.scrollToItem(0) }
    val matches = session.link?.endpoint?.fingerprint == project.fingerprint
    val canConnect = matches && session.link?.blockedReason == null
    val canRetry = canConnect && messages.any { it.delivery == Delivery.QUEUED || it.delivery == Delivery.RETRYABLE }
    val hasReceipts = messages.any { it.delivery == Delivery.RECEIVED }
    val colors = MaterialTheme.colorScheme
    Column(Modifier.widthIn(max = 680.dp).fillMaxSize()) {
        if (!matches) WorkshopNotice("此项目关联另一台电脑，重新连接该电脑后可发送。")
        connectionProblem(session)?.let { WorkshopNotice(it) }
        session.notice?.let { WorkshopNotice(it) }
        session.dialogueNotice?.let { WorkshopNotice(it) }
        if (canRetry || hasReceipts) {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 10.dp),
                horizontalArrangement = Arrangement.End,
            ) {
                if (canRetry) TextButton(enabled = !session.busy, onClick = session::retry) { Text("重试待发送消息") }
                if (hasReceipts) TextButton(enabled = canConnect && !session.busy, onClick = session::refreshDialogue) {
                    Text("刷新规格会话")
                }
            }
        }
        if (messages.isEmpty()) {
            Box(Modifier.weight(1f).fillMaxWidth().padding(horizontal = 24.dp), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Box(Modifier.size(60.dp).background(colors.primaryContainer, RoundedCornerShape(20.dp)), contentAlignment = Alignment.Center) {
                        WorkshopIcon(WorkshopGlyph.Spark, colors.onPrimaryContainer, Modifier.size(30.dp))
                    }
                    Text("开始描述你的插件", style = MaterialTheme.typography.titleLarge)
                    Text("可以先说说它要解决什么问题。", color = colors.onSurfaceVariant)
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth(),
                state = scroll,
                reverseLayout = true,
                contentPadding = PaddingValues(horizontal = 18.dp, vertical = 16.dp),
                verticalArrangement = Arrangement.spacedBy(18.dp, Alignment.Top),
            ) {
                items(messages.asReversed(), key = { it.id }) { item ->
                    WorkshopTurn(session, item, session.dialogueHeads[item.id], session.sendingId == item.id, project.id)
                }
            }
        }
        WorkshopComposer(
            body,
            onBodyChange = onBodyChange,
            enabled = !session.busy,
            onSend = {
                val submitted = body
                focus.clearFocus()
                session.enqueue(submitted, onSaved = { onSubmitted(submitted) })
            },
        )
    }
}

@Composable
private fun WorkshopTurn(
    session: DesktopConversationSession,
    message: QueuedMessage,
    event: JSONObject?,
    sending: Boolean,
    projectId: String,
) {
    val colors = MaterialTheme.colorScheme
    var details by rememberSaveable { mutableStateOf(false) }
    val delivery = when {
        sending -> "发送中"
        message.delivery == Delivery.RECEIVED -> "电脑已接收"
        message.delivery == Delivery.REJECTED -> "发送失败"
        message.delivery == Delivery.RETRYABLE && message.detail != "DESKTOP_OFFLINE" -> "发送失败，可重试"
        else -> "本机排队"
    }
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(7.dp)) {
        Surface(
            modifier = Modifier.align(Alignment.End).fillMaxWidth(0.85f),
            color = colors.primaryContainer,
            contentColor = colors.onPrimaryContainer,
            shape = RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp, bottomStart = 20.dp, bottomEnd = 6.dp),
        ) {
            SelectionContainer { Text(message.body, Modifier.padding(horizontal = 15.dp, vertical = 12.dp)) }
        }
        Row(
            Modifier.align(Alignment.End),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(delivery, style = MaterialTheme.typography.labelMedium, color = colors.onSurfaceVariant)
            TextButton(onClick = { details = !details }) { Text(if (details) "收起详情" else "消息详情") }
        }
        message.detail?.let {
            Text(desktopFailureText(it), Modifier.align(Alignment.End), style = MaterialTheme.typography.bodySmall, color = colors.error)
        }
        if (details) {
            SelectionContainer {
                Column(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("项目 ID $projectId", style = MaterialTheme.typography.bodySmall)
                    Text("消息 ID ${message.id}", style = MaterialTheme.typography.bodySmall)
                    message.receipt?.let {
                        Text("回执 ID ${it.id}", style = MaterialTheme.typography.bodySmall)
                        Text("电脑接收时间 ${it.receivedAt}", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
        if (event != null) {
            Row(
                Modifier.fillMaxWidth().padding(end = 14.dp),
                verticalAlignment = Alignment.Top,
                horizontalArrangement = Arrangement.spacedBy(9.dp),
            ) {
                Box(Modifier.size(33.dp).background(colors.primaryContainer, RoundedCornerShape(11.dp)), contentAlignment = Alignment.Center) {
                    WorkshopIcon(WorkshopGlyph.Spark, colors.onPrimaryContainer, Modifier.size(18.dp))
                }
                Surface(
                    modifier = Modifier.weight(1f),
                    color = colors.surface,
                    border = BorderStroke(1.dp, colors.outlineVariant),
                    shape = RoundedCornerShape(topStart = 6.dp, topEnd = 20.dp, bottomStart = 20.dp, bottomEnd = 20.dp),
                ) {
                    SelectionContainer {
                        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                            DesktopDialogueState(event)
                            if (message.delivery == Delivery.RECEIVED && event.optString("type") == "desktop.question") {
                                var answer by remember(event.optString("eventId")) { mutableStateOf("") }
                                OutlinedTextField(
                                    value = answer,
                                    onValueChange = { if (it.length <= 12_000) answer = it },
                                    label = { Text("补充信息") },
                                    modifier = Modifier.fillMaxWidth(),
                                    enabled = !session.busy,
                                    minLines = 2,
                                    maxLines = 5,
                                )
                                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Button(
                                        enabled = !session.busy && answer.isNotBlank(),
                                        onClick = { session.submitDialogue(message, event, "phone.answer", answer) },
                                    ) { Text("提交回答") }
                                    TextButton(
                                        enabled = !session.busy,
                                        onClick = { session.submitDialogue(message, event, "phone.cancel") },
                                    ) { Text("取消本轮") }
                                }
                            } else if (message.delivery == Delivery.RECEIVED && event.optString("type") == "desktop.review") {
                                var revision by remember(event.optString("eventId")) { mutableStateOf("") }
                                OutlinedTextField(
                                    value = revision,
                                    onValueChange = { if (it.length <= 12_000) revision = it },
                                    label = { Text("补充需求或要求修改") },
                                    modifier = Modifier.fillMaxWidth(),
                                    enabled = !session.busy,
                                    minLines = 2,
                                    maxLines = 5,
                                )
                                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Button(
                                        enabled = !session.busy && revision.isNotBlank(),
                                        onClick = { session.submitDialogue(message, event, "phone.revise", revision) },
                                    ) { Text("请求修订") }
                                    TextButton(
                                        enabled = !session.busy,
                                        onClick = { session.submitDialogue(message, event, "phone.cancel") },
                                    ) { Text("取消本轮") }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun WorkshopNotice(message: String) {
    val colors = MaterialTheme.colorScheme
    Surface(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
        shape = RoundedCornerShape(12.dp),
        color = colors.errorContainer,
        contentColor = colors.onErrorContainer,
    ) {
        Text(message, Modifier.padding(horizontal = 12.dp, vertical = 8.dp), style = MaterialTheme.typography.bodySmall)
    }
}
