# -*- coding: utf-8 -*-
"""
Created on Fri Apr  1 22:23:59 2022

@author: Alexander
"""
import random

def shuffle(players, state, allow_4_game = False, full_info = False, sub_sec = [], soft_max = False):
    #one round shufle
    
    forbiden = []
    chances_list = []
    
    if full_info:
        print("-----start shuffling------")
    
    for p in players:
        if p in state.played_games:
            if allow_4_game:
                if state.played_games[p] > 3:
                    forbiden.append(p)
            elif state.played_games[p] > 2:
                forbiden.append(p)
    
    if full_info:
        print("1:",  forbiden)
    
    ids_for_shuffle = []
    weights = []
    
    for p in players:
        if p not in forbiden:
            ids_for_shuffle.append(p)
            weights.append(calculate_weigth(p, state))

    if len(ids_for_shuffle) == 0:
        return ([], chances_list)
    
    
    if full_info:
        print(ids_for_shuffle, weights)
    
    if len(sub_sec) < 1:     
        player1 = random.choices(ids_for_shuffle, weights = weights)[0]
    else:
        player1 = sub_sec[0]
        
    #print(player1)
        
    if player1 in ids_for_shuffle:   
        chances_list.append(100*weights[ids_for_shuffle.index(player1)]/sum(weights))
    else:
        chances_list.append(0)

#----------
    
    
    forbiden += [player1]
    forbiden += state.twice_played_with[player1]

    if full_info:
        print("2:",  forbiden)

    ids_for_shuffle = []
    weights = []

    for p in players:
        if p not in forbiden:
            ids_for_shuffle.append(p)
            weights.append(calculate_weigth(p, state, [player1]))



    if len(ids_for_shuffle) == 0:
        return ([player1], chances_list)
    
    if full_info:
        print(ids_for_shuffle, weights)

    if len(sub_sec) < 2:     
        player2 = random.choices(ids_for_shuffle, weights = weights)[0]
    else:
        player2 = sub_sec[1]
        
    if player2 in ids_for_shuffle:   
        chances_list.append(100*weights[ids_for_shuffle.index(player2)]/sum(weights))
    else:
        chances_list.append(0)
    


    forbiden += [player2]
    forbiden += state.twice_played_with[player2]

    if full_info:
        print("3:",  forbiden)

    ids_for_shuffle = []
    weights = []

    for p in players:
        if p not in forbiden:
            ids_for_shuffle.append(p)
            weights.append(calculate_weigth(p, state, [player1, player2]))

    if len(ids_for_shuffle) == 0:
        return ([player1, player2], chances_list)
    
    
    if full_info:
        print(ids_for_shuffle, weights)

    if len(sub_sec) < 3:     
        player3 = random.choices(ids_for_shuffle, weights = weights)[0]
    else:
        player3 = sub_sec[2]
        
    if player3 in ids_for_shuffle:   
        chances_list.append(100*weights[ids_for_shuffle.index(player3)]/sum(weights))
    else:
        chances_list.append(0)

    forbiden += [player3]
    forbiden += state.twice_played_with[player3]

    if full_info:
        print("4:",  forbiden)

    ids_for_shuffle = []
    weights = []

    for p in players:
        if p not in forbiden:
            ids_for_shuffle.append(p)
            weights.append(calculate_weigth(p, state, [player1, player2, player3]))

    if len(ids_for_shuffle) == 0:
        return ([player1, player2, player3], chances_list)
    
    if len(sub_sec) < 4:     
        player4 = random.choices(ids_for_shuffle, weights = weights)[0]
    else:
        player4 = sub_sec[3]
        
    
    if full_info:
        print(ids_for_shuffle, weights)

    if player4 in ids_for_shuffle:   
        chances_list.append(100*weights[ids_for_shuffle.index(player4)]/sum(weights))
    else:
        chances_list.append(0)
    
    return ([player1, player2, player3, player4], chances_list)



def calculate_weigth(player, state, choused = [], soft_max = True, priority_list = []):
    if state.played_games[player] == 3:
        w = 1
    else:
        w = 3*(4-state.played_games[player])+state.total_games[player]+(4-state.played_games[player])//2*state.skipped_games[player] + 20*(player in priority_list)
    for p in choused:
        if p in state.played_with[player]:
            w *= 0.5
    if soft_max:
        return pow(10, w) 
    else:
        return w
    

    

def estimate_rate(player, state, choused = [], soft_max = True, priority_list = []):
    
    cur_rate = 0

    cur_rate -= state.played_games[player]*400

    cur_rate += state.total_games[player]*100


    if state.played_games[player] >= 3:
        cur_rate -= 10000

    for c in choused:
        if c in state.played_with[player]:
            cur_rate -= 1000

    return cur_rate


def estimate_rates(players, state, choused = [], soft_max = True, priority_list = []):
    return {x:estimate_rate(x, state, choused, soft_max, priority_list) for x in players}

