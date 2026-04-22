// bracket-renderer.js – универсальный рендерер турнирной сетки
// с поддержкой детальных карточек (очки, баллы) для завершённых матчей

function renderBracket(schema, completedMatches, container, options = {}) {
    const containerEl = typeof container === 'string' ? document.getElementById(container) : container;
    if (!containerEl) return;
    const activePlayers = options.activePlayers || []; // массив имён
    // Глубокая копия матчей с состоянием
    console.log("d", schema.matches)

    const matches = schema.matches.map(m => ({
        id: m.id,
        name: m.name || m.id,
        participantSlots: m.participantSlots,
        sources: m.sources || [],
        virtual: m.virtual === true,
        type: m.type || 'match',
        aggregateBy: m.aggregateBy || 'sum',
        outputSlots: m.outputSlots || null,
        defaultRanking: m.defaultRanking || null,
        participants: new Array(m.participantSlots).fill(null),
        results: null,
        completedData: null,
        completed: false,
        participants: new Array(m.participantSlots).fill(null)
    }));


    matches.forEach(m => {
        if (m.type === 'seed') {
            for (let i = 0; i < Math.min(m.participantSlots, activePlayers.length); i++) {
                m.participants[i] = activePlayers[i];
            }
            // остальные слоты остаются null
        } else {
            // для обычных матчей заполняем из sources типа 'player'
            m.sources.forEach(src => {
                if (src.type === 'player') m.participants[src.slot-1] = src.name;
            });
        }
    });

    // ---- Вспомогательные функции (те же, что и ранее) ----
    function getMatchCompletedData(matchId) {
        return completedMatches.find(cm => cm.gameindex == matchId);
    }

    function applyResults() {
        
        matches.forEach(m => {
            if (m.virtual && !m.results && m.type !== 'aggregator') {
                let order = m.participants.slice();
                if (m.defaultRanking) {
                    order = m.defaultRanking.map(slot => m.participants[slot-1]);
                }
                m.results = order.filter(p => p);
                m.completed = true;
            }
        });

        matches.forEach(m => {
            if (m.type === 'seed') return;
            if (m.results) return;
            if (m.type === 'aggregator') return;
            const apiData = getMatchCompletedData(m.id);
            if (apiData && apiData.scores && apiData.scores.length === m.participantSlots) {
                m.completedData = apiData;
                m.completed = true;
                const sorted = [...apiData.scores].sort((a,b) => a.position - b.position);
                m.results = sorted.map(s => s.name);
                for (let i = 0; i < sorted.length; i++) {
                    m.participants[i] = sorted[i].name;
                }
            }
        });
    }

    function computeAggregator(agg) {
        const playerPoints = new Map();
        let allSourcesCompleted = true;
        for (let src of agg.sources) {
            if (src.type !== 'match') continue;
            const sourceMatch = matches.find(m => m.id === src.matchId);
            if (!sourceMatch || !sourceMatch.completed) {
                allSourcesCompleted = false;
                break;
            }
            const scores = sourceMatch.completedData?.scores;
            if (scores) {
                scores.forEach(score => {
                    const points = (src.field === 'points') ? (score.points || 0) : (score.score || 0);
                    const current = playerPoints.get(score.name) || 0;
                    playerPoints.set(score.name, current + points);
                });
            }
        }
        if (!allSourcesCompleted) return null;
        const sorted = Array.from(playerPoints.entries())
            .map(([name, total]) => ({ name, total }))
            .sort((a,b) => b.total - a.total);
        let results = sorted.map(item => item.name);
        if (agg.participantSlots && agg.participantSlots < results.length) {
            results = results.slice(0, agg.participantSlots);
        }
        const fakeScores = results.map((name, idx) => ({
            name: name,
            position: idx + 1,
            score: 0,
            points: sorted.find(s => s.name === name)?.total || 0
        }));
        return { results, participants: results.slice(), fakeScores };
    }

    function updateAggregators() {
        let changed = true;
        while (changed) {
            changed = false;
            for (let agg of matches.filter(m => m.type === 'aggregator')) {
                if (agg.completed) continue;
                const computed = computeAggregator(agg);
                if (computed) {
                    agg.results = computed.results;
                    agg.participants = computed.participants;
                    agg.completed = true;
                    agg.completedData = { gameindex: agg.id, scores: computed.fakeScores };
                    changed = true;
                }
            }
        }
    }

    function buildReverseLinks() {
        const map = new Map();
        matches.forEach(m => {
            m.sources.forEach(src => {
                if (src.type === 'match') {
                    if (!map.has(src.matchId)) map.set(src.matchId, []);
                    map.get(src.matchId).push({
                        targetMatchId: m.id,
                        targetSlot: src.slot,
                        place: src.place
                    });
                }
            });
        });
        return map;
    }

function propagateParticipants(reverseLinks) {
    let changed = true;
    while (changed) {
        changed = false;
        for (let m of matches) {
            // Распространяем только если:
            // - это seed-матч, ИЛИ
            // - матч завершён (completed === true)
            const canPropagate = (m.type === 'seed') || (m.completed === true);
            if (!canPropagate) continue;
            
            // Если у матча нет участников – пропускаем
            if (!m.participants || m.participants.every(p => !p)) continue;
            
            const dependents = reverseLinks.get(m.id) || [];
            for (let dep of dependents) {
                const target = matches.find(t => t.id === dep.targetMatchId);
                if (target && !target.completed) {
                    const playerName = m.participants[dep.place - 1];
                    if (playerName && target.participants[dep.targetSlot - 1] !== playerName) {
                        target.participants[dep.targetSlot - 1] = playerName;
                        changed = true;
                    }
                }
            }
        }
    }
}
    

    function computeLevels() {
        const levels = new Map();
        function getLevel(matchId) {
            if (levels.has(matchId)) return levels.get(matchId);
            const match = matches.find(m => m.id === matchId);
            if (!match) return 0;
            let maxLevel = -1;
            match.sources.forEach(src => {
                if (src.type === 'match') {
                    const lvl = getLevel(src.matchId);
                    if (lvl > maxLevel) maxLevel = lvl;
                }
            });
            const level = maxLevel + 1;
            levels.set(matchId, level);
            return level;
        }
        matches.forEach(m => getLevel(m.id));
        return levels;
    }

    function groupByLevels(levels) {
        const groups = new Map();
        matches.forEach(m => {
            const lvl = levels.get(m.id);
            if (!groups.has(lvl)) groups.set(lvl, []);
            groups.get(lvl).push(m);
        });
        const sortedLevels = Array.from(groups.keys()).sort((a,b) => a-b);
        return sortedLevels.map(lvl => groups.get(lvl));
    }
    
    function renderSeedCard(match) {
        const card = document.createElement('div');
        card.className = 'seed-card';
        card.innerHTML = `<div class="match-title">${escapeHtml(match.name)}</div>`;
        const list = document.createElement('div');
        list.className = 'seed-players-list';
        for (let i = 0; i < match.participantSlots; i++) {
            const player = match.participants[i] || '—';
            list.innerHTML += `
                <div class="seed-player-row">
                    <span class="seed-slot">${i+1}</span>
                    <span class="seed-name">${escapeHtml(player)}</span>
                </div>
            `;
        }
        card.appendChild(list);
        if (!options.readOnly && options.onSeedUpdate) {
            const refreshBtn = document.createElement('button');
            refreshBtn.textContent = '🔄 Обновить посев';
            refreshBtn.className = 'seed-refresh-btn';
            refreshBtn.onclick = () => options.onSeedUpdate();
            card.appendChild(refreshBtn);
        }
        return card;
    }
    // ========== НОВАЯ ФУНКЦИЯ РЕНДЕРА КАРТОЧКИ (с очками и баллами) ==========
    function renderMatchCard(match) {

        if (match.type === 'seed') {
            return renderSeedCard(match);
        }
        // Если матч завершён и есть детальные данные – используем красивую карточку
        if (match.completed && match.completedData && match.completedData.scores) {
            const game = match.completedData;
            const card = document.createElement('div');
            card.className = 'game-card past';
            
            // Сортируем игроков по занятым местам
            const sortedPlayers = [...game.scores].sort((a, b) => a.position - b.position);
            
            let playersHtml = '';
            sortedPlayers.forEach(player => {
                const scoreClass = player.score >= 0 ? 'positive-score' : 'negative-score';
                playersHtml += `
                    <div class="gc-player-row">
                        <span class="gc-player-place place-${player.position}">${player.position}</span>
                        <span class="gc-player-name" title="${player.name}">${escapeHtml(player.name)}</span>
                        <span class="gc-player-score ${scoreClass}">${player.score > 0 ? '+' : ''}${player.score}</span>
                        <span class="gc-player-points">${(player.points || 0).toFixed(2)}</span>
                    </div>
                `;
            });
            
            card.innerHTML = `
                <div class="game-header">
                    <span class="gc-game-id">${escapeHtml(match.name)}</span>
                    <span class="gc-game-type">${match.type === 'aggregator' ? 'агрегатор' : (match.virtual ? 'виртуальная' : 'обычная')}</span>
                </div>
                <div class="gc-players-container">
                    <div class="gc-players-column">
                        ${playersHtml}
                    </div>
                </div>
                <div class="game-footer">
                    ${match.type === 'aggregator' ? '📊 Сумма баллов' : '✔ Завершена'}
                </div>
            `;
            
            // Добавляем кнопки действий (если нужно)
            if (!options.readOnly && match.type !== 'aggregator' && !match.virtual) {
                const editBtn = document.createElement('button');
                editBtn.textContent = '✏️ Редактировать';
                editBtn.className = 'match-action-btn';
                editBtn.onclick = (e) => {
                    e.stopPropagation();
                    if (options.onPlayMatch) options.onPlayMatch(match.id, match.participants.slice(), true);
                };
                card.querySelector('.game-footer').appendChild(editBtn);
            }
            
            if (options.onShowDetails) {
                const detailsBtn = document.createElement('button');
                detailsBtn.textContent = '📋 Детали';
                detailsBtn.className = 'match-details-btn';
                detailsBtn.onclick = (e) => {
                    e.stopPropagation();
                    options.onShowDetails(match.id, game);
                };
                card.querySelector('.game-footer').appendChild(detailsBtn);
            }
            
            return card;
        }
        
        // ---------- Для незавершённых или виртуальных матчей (без данных) используем упрощённую карточку ----------
        const card = document.createElement('div');
        card.className = `match-card ${match.virtual ? 'virtual' : ''} ${match.type === 'aggregator' ? 'aggregator' : ''}`;
        
        const title = document.createElement('div');
        title.className = 'match-title';
        title.textContent = match.name;
        card.appendChild(title);
        
        const participantsDiv = document.createElement('div');
        participantsDiv.className = 'match-participants';
        for (let i = 0; i < match.participantSlots; i++) {
            const slot = document.createElement('div');
            slot.className = 'participant-slot';
            let name = match.participants[i];
            if (!name && match.results && match.results[i]) name = match.results[i];
            slot.textContent = name || '—';
            participantsDiv.appendChild(slot);
        }
        card.appendChild(participantsDiv);
        
        // Кнопка "Играть" для доступных матчей
        if (!options.readOnly) {
            const isPlayable = !match.completed && match.type !== 'aggregator' && !match.virtual && match.participants.every(p => p && p !== '—');
            if (isPlayable) {
                const playBtn = document.createElement('button');
                playBtn.textContent = '🎮 Играть';
                playBtn.className = 'match-action-btn';
                playBtn.onclick = (e) => {
                    e.stopPropagation();
                    if (options.onPlayMatch) options.onPlayMatch(match.id, match.participants.slice(), false);
                };
                card.appendChild(playBtn);
            }
        }
        
        return card;
    }

    // Экранирование HTML (для безопасности)
    function escapeHtml(str) {
        if (!str) return '';
        return str.replace(/[&<>]/g, function(m) {
            if (m === '&') return '&amp;';
            if (m === '<') return '&lt;';
            if (m === '>') return '&gt;';
            return m;
        });
    }

    // ---- Основной процесс ----
    applyResults();
    updateAggregators();
    const reverseLinks = buildReverseLinks();
    propagateParticipants(reverseLinks);
    const levels = computeLevels();
    const rounds = groupByLevels(levels);
    
    containerEl.innerHTML = '';
    rounds.forEach(roundMatches => {
        const roundDiv = document.createElement('div');
        roundDiv.className = 'bracket-round';
        roundMatches.forEach(match => {
            roundDiv.appendChild(renderMatchCard(match));
        });
        containerEl.appendChild(roundDiv);
    });
}

// Экспорт (если используется модульная система)
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { renderBracket };
} else {
    window.renderBracket = renderBracket;
}