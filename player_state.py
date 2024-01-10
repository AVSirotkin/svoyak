# -*- coding: utf-8 -*-
"""
Created on Thu Jun  9 10:57:35 2022

@author: Alexander
"""
from copy import deepcopy
import random
from shuffle import *

class player_state:
    skipped_games = {}
    total_games = {}
    played_games = {}
    twice_played_with = {}
    played_with = {}
    history = []
    active_players = []
    
    def get_copy(self):
        a = player_state()
        a.skipped_games = deepcopy(self.skipped_games)
        a.total_games = deepcopy(self.total_games)
        a.played_games = deepcopy(self.played_games)
        a.twice_played_with = deepcopy(self.twice_played_with)
        a.played_with = deepcopy(self.played_with)
        a.history = deepcopy(self.history)
        a.active_players = deepcopy(self.active_players)
        return a
    
    def shuffle_players(self, fixed = [], seed = 0, full_info = False, by_rate = True):
        random.seed(seed)
        print("DDD", self.active_players)
        
        if by_rate:
            player_in_game = list(fixed)
            while len(player_in_game)<4:
                rt = estimate_rates(self.active_players, self, choused = player_in_game)
                if len(rt) == 0:
                    break
                else:
                    s_rate = sorted(rt.items(), key=lambda x:-x[1])
                    player_in_game.append(s_rate[0][0])
            return player_in_game
        else:
            print("DDD", self.active_players)
            
            for i in range(100):
                player_in_game, chances = shuffle(self.active_players, self, sub_sec = fixed, full_info = full_info)
                if len(player_in_game) == 4:
                    break
            if len(player_in_game) < 4:
                print(self.played_games, self.skipped_games)
                for i in range(100):
                    player_in_game, chances = shuffle(self.active_players, self, sub_sec = fixed, allow_4_game=True, full_info = full_info)
                    if len(player_in_game) == 4:
                        break
            print(player_in_game, chances)
            # my_log(str(player_in_game) +" " +str(chances))
            print(self.active_players)
            
            return player_in_game
    
    def process_one_match(self, player_in_game):
        self.history.append(player_in_game)
       
        #update restrictions
        for i in player_in_game:
            self.played_games[i] += 1
            self.skipped_games[i] = 0
            
            for j in player_in_game:
                if i != j:
                    if i in self.played_with[j]:
                        self.twice_played_with[j].append(i)
                    else:
                        self.played_with[j].append(i)
        
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
            self.twice_played_with[p] = []
            self.played_with[p] = []
        if not p in self.active_players:
            self.active_players.append(p)
            
            
    def remove_player(self, pl_name):
        self.active_players.remove(pl_name)
        
