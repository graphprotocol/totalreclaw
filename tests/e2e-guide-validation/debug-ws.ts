import WebSocket from 'ws';

const ws = new WebSocket('ws://127.0.0.1:18789');
let connected = false;

ws.on('open', () => {
  ws.send(JSON.stringify({
    type: 'req', id: 'r1', method: 'connect',
    params: {
      minProtocol: 3, maxProtocol: 3,
      client: { id: 'cli', displayName: 'test', version: 'dev', platform: 'node', mode: 'cli' },
      role: 'operator', scopes: ['operator.read', 'operator.write'],
      auth: { token: 'guide-test-token-2026' },
    },
  }));
});

ws.on('message', (data) => {
  const msg = JSON.parse(String(data));

  if (msg.type === 'res' && msg.id === 'r1' && msg.ok && !connected) {
    connected = true;
    console.log('Connected. Sending chat message...');
    ws.send(JSON.stringify({
      type: 'req', id: 'r2', method: 'chat.send',
      params: {
        sessionKey: 'tool-test-' + Date.now(),
        message: 'Use the totalreclaw_recall tool to search for memories about "favorite color". Show me exactly what the tool returns.',
        idempotencyKey: 'test-' + Date.now(),
      },
    }));
    return;
  }

  // Log ALL message types, not just events
  console.log(`[type=${msg.type}] ${JSON.stringify(msg).substring(0, 1200)}`);

  if (msg.type === 'event') {
    const p = msg.payload || {};

    if (p.state === 'final' || p.state === 'error') {
      const text = p.message?.content?.map((b: any) => b.text || JSON.stringify(b)).join('') || '';
      console.log('\n=== FINAL RESPONSE ===');
      console.log(text);
      console.log('=== END ===');
      setTimeout(() => { ws.close(); process.exit(0); }, 2000);
    }
  }
});

setTimeout(() => {
  console.log('Timeout.');
  ws.close();
  process.exit(1);
}, 120000);
