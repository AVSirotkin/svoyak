# -*- coding: utf-8 -*-
"""
Created on Thu Jun  9 10:57:35 2022

@author: Alexander
"""
from copy import deepcopy
import random
from shuffle import *

class player_state:
    def __init__(self):
        self.skipped_games = {}
        self.total_games = {}
        self.played_games = {}
        self.played_with = {}
        self.history = []
        self.active_players = []
    
    def get_copy(self):
        a = player_state()
        a.skipped_games = deepcopy(self.skipped_games)
        a.total_games = deepcopy(self.total_games)
        a.played_games = deepcopy(self.played_games)
        a.played_with = deepcopy(self.played_with)
        a.history = deepcopy(self.history)
        a.active_players = deepcopy(self.active_players)
        return a
    
    def shuffle_players(self, fixed = [], forbiden = [], seed = 0, full_info = False, by_rate = True, rules = "Spontan"):
        random.seed(seed)
        # print("DDD", self.active_players)
        available_players = [x for x in self.active_players if not x in forbiden]
        
        if by_rate:
            player_in_game = list(fixed)
            while len(player_in_game)<4:
                rt = estimate_rates(available_players, self, choused = player_in_game, rules=rules)
                if len(rt) == 0:
                    break
                else:
                    s_rate = sorted(rt.items(), key=lambda x:-x[1])
                    player_in_game.append(s_rate[0][0])
            return player_in_game
        else:
            print("DDD random", self.active_players)
            
            for i in range(100):
                player_in_game, chances = shuffle(available_players, self, sub_sec = fixed, full_info = full_info)
                if len(player_in_game) == 4:
                    break
            if len(player_in_game) < 4:
                print(self.played_games, self.skipped_games)
                for i in range(100):
                    player_in_game, chances = shuffle(available_players, self, sub_sec = fixed, allow_4_game=True, full_info = full_info)
                    if len(player_in_game) == 4:
                        break
            print(player_in_game, chances)
            # my_log(str(player_in_game) +" " +str(chances))
            print(self.active_players)
            
            return player_in_game
    
    def process_one_match(self, player_in_game):
        self.history.append(player_in_game)

        for i in player_in_game:
            if not i in self.skipped_games:
                self.add_player(i)

       
        #update restrictions
        for i in player_in_game:
            self.played_games[i] += 1
            self.skipped_games[i] = 0
            
            for j in player_in_game:
                if i != j:
                    if not j in self.played_with:
                        self.played_with[j] = {} 
                    if i in self.played_with[j]:
                        self.played_with[j][i] += 1
                    else:
                        self.played_with[j][i] = 1
        
        for i in self.active_players:
            self.total_games[i] += 1
            if i not in player_in_game:
                self.skipped_games[i] += 1


    def add_player(self, p):
        if p not in self.skipped_games:      
            self.skipped_games[p] = 1
            self.total_games[p] = 0
            self.played_games[p] = 0
#            players += 1
            self.played_with[p] = {}
        if not p in self.active_players:
            self.active_players.append(p)
            
            
    def remove_player(self, pl_name):
        if pl_name in self.active_players:
            self.active_players.remove(pl_name)
        
