const { createApp, ref, onMounted, watch, nextTick } = Vue;

const app = createApp({
    setup() {
        const currentRoute = ref('chat');
        const selectedKbId = ref('agv_demo');
        const knowledgeBases = ref([{ kb_id: 'agv_demo', name: 'AGV 默认知识库' }]);
        const userInput = ref('');
        const messages = ref([]);
        const isStreaming = ref(false);
        const streamingStatus = ref('');
        const expandedSources = ref({});
        const msgContainer = ref(null);

        const handleRoute = () => {
            const hash = window.location.hash.replace('#/', '') || 'chat';
            currentRoute.value = hash;
        };

        const scrollToBottom = async () => {
            await nextTick();
            if (msgContainer.value) {
                msgContainer.value.scrollTop = msgContainer.value.scrollHeight;
            }
        };

        const renderMarkdown = (text) => {
            if (!text) return '';
            return DOMPurify.sanitize(marked.parse(text));
        };

        const toggleSource = (id) => {
            expandedSources.value[id] = !expandedSources.value[id];
        };

        const clearChat = () => {
            messages.value = [];
        };

        const fetchKBs = async () => {
            try {
                const resp = await fetch('/api/kb');
                const json = await resp.json();
                if (json.data) {
                    knowledgeBases.value = json.data;
                }
            } catch (e) {
                console.error('Failed to fetch KBs:', e);
            }
        };

        const sendMessage = async () => {
            if (!userInput.value.trim() || isStreaming.value) return;

            const question = userInput.value.trim();
            messages.value.push({ role: 'user', content: question });
            userInput.value = '';

            // Prepare AI message slot
            const aiMsgIdx = messages.value.length;
            messages.value.push({
                role: 'ai',
                content: '',
                sources: null
            });

            isStreaming.value = true;
            await scrollToBottom();

            try {
                const response = await fetch('/api/chat/stream', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        question,
                        kb_id: selectedKbId.value
                    })
                });

                if (!response.ok) throw new Error('Network response was not ok');

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                while (true) {
                    const { value, done } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n\n');
                    buffer = lines.pop();

                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            const dataStr = line.slice(6);
                            try {
                                const event = JSON.parse(dataStr);

                                if (event.type === 'status') {
                                    streamingStatus.value = event.content;
                                } else if (event.type === 'chunk') {
                                    messages.value[aiMsgIdx].content += event.content;
                                    await scrollToBottom();
                                } else if (event.type === 'sources') {
                                    messages.value[aiMsgIdx].sources = event.content.sources;
                                } else if (event.type === 'error') {
                                    messages.value[aiMsgIdx].content += `\n\n**错误**: ${event.content}`;
                                } else if (event.type === 'done') {
                                    streamingStatus.value = '';
                                }
                            } catch (e) {
                                console.error('JSON parse error:', e, dataStr);
                            }
                        }
                    }
                }
            } catch (e) {
                console.error('Chat error:', e);
                messages.value[aiMsgIdx].content += `\n\n**系统错误**: ${e.message}`;
            } finally {
                isStreaming.value = false;
                streamingStatus.value = '';
                await scrollToBottom();
            }
        };

        onMounted(() => {
            handleRoute();
            window.addEventListener('hashchange', handleRoute);
            fetchKBs();
        });

        return {
            currentRoute,
            selectedKbId,
            knowledgeBases,
            userInput,
            messages,
            isStreaming,
            streamingStatus,
            expandedSources,
            msgContainer,
            renderMarkdown,
            toggleSource,
            clearChat,
            sendMessage
        };
    }
});

app.mount('#app');
