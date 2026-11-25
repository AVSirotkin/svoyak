// Проверка правил «событие только по значимому изменению»: подпись состояния
// не должна зависеть от указателя, режима и клиента, а повтор — уходить.
//
// Запуск: node tests/js/check_signature.js (см. tests/test_sync_rules.py).
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..', '..');

function extract(source, name) {
    const marker = 'function ' + name + '(';
    const start = source.indexOf(marker);
    if (start < 0) throw new Error('нет функции ' + name);
    let depth = 0;
    for (let i = start; i < source.length; i += 1) {
        if (source[i] === '{') depth += 1;
        if (source[i] === '}') {
            depth -= 1;
            if (depth === 0) return source.slice(start, i + 1);
        }
    }
    throw new Error('не закрыта функция ' + name);
}

const problems = [];

function check(label, condition, detail) {
    if (!condition) problems.push(label + (detail ? ': ' + detail : ''));
}

// ---------------------------------------------------------------- FullRoom
const room = fs.readFileSync(path.join(root, 'templates', 'FullRoom.html'), 'utf8');
const roomScript = [
    extract(room, 'layoutSignature'),
    extract(room, 'protocolSignature'),
    extract(room, 'queueProtocol'),
].join('\n');

const roomState = {
    payload: null,
    queued: [],
};
const roomSandbox = {
    protocolLastPayload: {},
    gameData: {questionBlocks: [{theme: 'Слова французского происхождения', questions: [{value: 100, text: 'Вопрос', answer: 'Ответ'}]}]},
    Offline: {enqueue: (type, payload) => roomState.queued.push(payload), mode: () => 'sync'},
    Number, JSON, Object, String,
};
roomSandbox.protocolPayload = () => JSON.parse(JSON.stringify(roomState.payload));
roomSandbox.getCurrentMatchId = () => 5;
vm.createContext(roomSandbox);
vm.runInContext(roomScript + '\nthis.protocolSignature = protocolSignature; this.queueProtocol = queueProtocol;', roomSandbox);

function basePayload() {
    return {
        gameindex: 5,
        marks: {'0:19': 'plus'},
        pointer: 1,
        players: [{playerid: 19, name: 'Ковба', place: 1}, {playerid: 22, name: 'Паутов', place: 2}],
        scores: {'19': 100, '22': 0},
        halfMinus: false,
        finished: false,
        mode: 'sync',
        client: 'c1',
        tab: 't1',
        base_revision: 3,
    };
}

roomState.payload = basePayload();
roomSandbox.queueProtocol(5);
check('первое сохранение ставится в очередь', roomState.queued.length === 1, 'очередь ' + roomState.queued.length);

roomSandbox.queueProtocol(5);
check('повтор без правок не уходит', roomState.queued.length === 1, 'очередь ' + roomState.queued.length);

roomState.payload = Object.assign(basePayload(), {pointer: 7});
roomSandbox.queueProtocol(5);
check('указатель — не событие', roomState.queued.length === 1, 'очередь ' + roomState.queued.length);

roomState.payload = Object.assign(basePayload(), {pointer: 7, mode: 'limited', client: 'c2', tab: 't2', base_revision: 9});
roomSandbox.queueProtocol(5);
check('режим и клиент — не событие', roomState.queued.length === 1, 'очередь ' + roomState.queued.length);

roomState.payload = Object.assign(basePayload(), {pointer: 2, marks: {'0:19': 'plus', '0:22': 'minus'}});
roomSandbox.queueProtocol(5);
check('отметка — событие', roomState.queued.length === 2, 'очередь ' + roomState.queued.length);

roomState.payload = Object.assign(basePayload(), {pointer: 2, marks: {'0:19': 'plus', '0:22': 'minus'}, scores: {'19': 100, '22': -20}});
roomSandbox.queueProtocol(5);
check('счёт — событие', roomState.queued.length === 3, 'очередь ' + roomState.queued.length);

roomState.payload = Object.assign(basePayload(), {pointer: 2, marks: {'0:19': 'plus', '0:22': 'minus'}, scores: {'19': 100, '22': -20}, finished: true});
roomSandbox.queueProtocol(5);
check('завершение боя — событие', roomState.queued.length === 4, 'очередь ' + roomState.queued.length);

roomSandbox.queueProtocol(5, {force: true});
check('force отправляет и без изменений', roomState.queued.length === 5, 'очередь ' + roomState.queued.length);

roomSandbox.gameData.questionBlocks[0].questions[0].text = 'Исправленный вопрос';
roomState.payload = Object.assign(basePayload(), {pointer: 2, marks: {'0:19': 'plus', '0:22': 'minus'}, scores: {'19': 100, '22': -20}, finished: true});
roomSandbox.queueProtocol(5);
check('правка текста темы — событие', roomState.queued.length === 6, 'очередь ' + roomState.queued.length);

// -------------------------------------------------------------- plan_table
const table = fs.readFileSync(path.join(root, 'templates', 'plan_table.html'), 'utf8');
const tableScript = [
    extract(table, 'protocolSignature'),
    extract(table, 'saveProtocol'),
].join('\n');

const tableState = {queued: [], payload: null};
const tableSandbox = {
    TOPICS: [{topic: 'Тема', questions: [{value: 100, text: 'Вопрос', answer: 'Ответ'}]}],
    GAMEINDEX: 3,
    conflictNote: '',
    lastSignature: '',
    rememberLocally: () => {},
    sync: {enqueue: (type, payload) => tableState.queued.push(payload), mode: () => 'sync'},
    Number, JSON, Object, String,
};
tableSandbox.protocolPayload = () => JSON.parse(JSON.stringify(tableState.payload));
vm.createContext(tableSandbox);
vm.runInContext(tableScript + '\nthis.saveProtocol = saveProtocol;', tableSandbox);

function tablePayload(pointer, extra) {
    return Object.assign({
        gameindex: 3,
        marks: {'0:19': 'plus'},
        pointer: pointer,
        players: [{playerid: 19, name: 'Ковба', place: 1}],
        scores: {'19': 100},
        halfMinus: false,
        finished: false,
        base_revision: 4,
        mode: 'sync',
        client: 'c1',
        tab: 't1',
    }, extra || {});
}

tableState.payload = tablePayload(1);
tableSandbox.saveProtocol();
check('площадка: первое сохранение уходит', tableState.queued.length === 1, 'очередь ' + tableState.queued.length);

tableState.payload = tablePayload(4);
tableSandbox.saveProtocol();
check('площадка: смена указателя — не событие', tableState.queued.length === 1, 'очередь ' + tableState.queued.length);

tableState.payload = tablePayload(4, {marks: {'0:19': 'minus'}});
tableSandbox.saveProtocol();
check('площадка: отметка — событие', tableState.queued.length === 2, 'очередь ' + tableState.queued.length);

tableState.payload = tablePayload(4, {marks: {'0:19': 'minus'}, halfMinus: true});
tableSandbox.saveProtocol();
check('площадка: половинный минус — событие', tableState.queued.length === 3, 'очередь ' + tableState.queued.length);

tableSandbox.saveProtocol({force: true});
check('площадка: force отправляет', tableState.queued.length === 4, 'очередь ' + tableState.queued.length);

if (problems.length) {
    console.log('ПРОБЛЕМЫ:\n' + problems.join('\n'));
    process.exit(1);
}
console.log('ИТОГ: события создаются только значимыми изменениями');
