# -*- coding: utf-8 -*-
"""
Created on Thu Mar 31 23:11:17 2022

@author: Alexander
"""

from tkinter import *
from tkinter import ttk
from tkinter.messagebox import askyesno, askquestion

from copy import deepcopy

import datetime as dt

import clipboard as clb

import random
from player_state import player_state
import json

import queue

from Sinchronize import my_sinch


sinch = my_sinch()

game_id = 1

sinch.init_connection()

all_players_info = sinch.all_players_info
today_sheet = sinch.today_sheet

player_lines = {}
total_players = 0



pl_list  = []
max_id = 0
lines = len(all_players_info)
rates = {}
print(all_players_info)

for d in all_players_info:
    print(d)
    if not (d["to_record"] == ''):
        if max_id < int(d["Id"]):
            max_id = d["Id"]
        pl_list.append(d["to_record"])
        rates[d["to_record"]] = d["Рейтинг"]
    
        

exec(open('player_state.py').read())


main_state = player_state()

full_info = False 

exec(open('shuffle.py').read())

all_player_list = sorted(pl_list)


def predict():
    state = main_state.get_copy()
    my_log("====PREDICT======")
    print("predict:", state.played_with)
    pre = []
    for i in range(4):
        if players_cb[i].get() in state.active_players:
            pre += [players_cb[i].get()]

    player_in_game = main_state.shuffle_players(pre)
    state.process_one_match(player_in_game)
    
    print("predict:", state.played_games)
    my_log(str(player_in_game))
    
    
    player_in_game = state.shuffle_players()
    state.process_one_match(player_in_game)
    my_log(str(player_in_game))
    
    
    player_in_game = state.shuffle_players()
    state.process_one_match(player_in_game)
    my_log(str(player_in_game))
    my_log("=================")

    
    reit = estimate_rates(main_state.active_players, main_state, choused = pre)
    s_reit = sorted(reit.items(), key=lambda x:-x[1])
    my_log(str(s_reit))


def write_log(s, f_name = "full_log.txt" ):
    f = open(f_name, "a")
    f.write(str(dt.datetime.now())+": "+s+"\n")
    f.close()


def on_select(*args):
    #Замена
    print(args)
    pass    



def clear_pl():
    for c in players_cb:
        c.set("Свободно")

def my_log(s):
    print(s)
    full_log.insert(END, s+"\n")


def fullfill():
    sub = []
    pre = []
    
    for i in range(4):
        if players_cb[i].get() in main_state.active_players:
            pre += [players_cb[i].get()]
            sub.append(i)
            
    player_in_game = main_state.shuffle_players(pre)
    
    n = 0
    
    for i in range(len(sub), len(player_in_game)):
        while n in sub:
            n += 1
        print(n,i)
        players_cb[n].set(player_in_game[i])
        n += 1
        
    predict()
        

def save_match():
#    combo1['values'] = ["Заполнил"]
#    combo1.current(0)
    
    if not askyesno("Подтверждение","Вносим результаты и переходим к новому раунду?"):
        return
    write_log("Сохранили результат")

    global game_id
    player_in_game = []
    
    for c in players_cb:
        if c.get() in main_state.active_players:
            player_in_game.append(c.get())
            
            
    if len(player_in_game) < 4:
        return()
    
    
    
    main_state.process_one_match(player_in_game)
    
    f = open("log.txt", "a")
    f.write("Игра "+str(game_id) + "\n")
    s = ""
    for i in range(4):
        today_sheet.update_cell(game_id*8-4+i,15, player_in_game[i])
        today_sheet.update_cell(game_id*8-4+i,16, ins[i].get())
        s += player_in_game[i] + "\t"+str(ins[i].get()) + "\n"
        f.write(player_in_game[i] + "\t"+str(ins[i].get()) + "\n")
        my_log(player_in_game[i] + "\t"+str(ins[i].get()))
    f.close()
    clb.copy(s)
    game_id += 1
    game_id_label["text"] = "Игра "+str(game_id)




def add_player():
    global active_players
    global total_players
    global game_id
    global player_lines

    val = combo_pl.get()
    if val == "":
        return
    if not val in players_list.get(0,100):
        write_log("Добавили: "+val)

        players_list.insert(END, val)
        
        
        #active_players = players_list.get(0,100)
        if not val in main_state.skipped_games:
            total_players += 1
            today_sheet.update_cell(3+total_players, 3, val)
            today_sheet.update_cell(3+total_players, 49, game_id)
            player_lines[val] = total_players

            if not val in rates:
                rates[val] = 1000
#            today_sheet.update_cell(3+total_players, 11, rates[val])
            
        main_state.add_player(val)
        

    for c in players_cb:
        c["values"] = ["Свободно"] + sorted(main_state.active_players)
    
    filter_var.set("")
    

        
        
    
def remove_player():
    global active_players
    global game_id
    idx = players_list.curselection()
#    print(len(players_list.curselection()))
    if len(idx) == 1:
        pl_name = players_list.get(idx[0])
        
        write_log("Убрали: "+str(pl_name))
        main_state.remove_player(pl_name)
        players_list.delete(idx[0])
        today_sheet.update_cell(3+player_lines[pl_name], 50, game_id)
 
    active_players = players_list.get(0,100)
    for c in players_cb:
        c["values"] = ["Свободно"] + sorted(active_players)


#def set_uset_pos():
#    print(change_places)
#    if 'selected' in change_places.state():
        
        
def on_field_change(*args):
    print(args)
    chk = filter_var.get().lower()
    if chk:
        combo_pl['values'] = [x for x in all_player_list if (chk in x.lower())]
    else:
        combo_pl['values'] = all_player_list
    pass


came_at_game_number = {}
leave_at_game_number = {}
seed_at_game_number = {} #for possible hand maintance
priority_list = []


app = Tk()

game_id_label = Label(app, text = "Игра номер "+str(game_id))
game_id_label.pack()
window = LabelFrame(app, text="Текущая игра" )
#window.title("Простейший интерфейс свояка")
window.pack(padx=10, pady=5)

active = LabelFrame(app, text="Активные игроки" )
#window.title("Простейший интерфейс свояка")
active.pack(padx=10, pady=5, side=LEFT)

filter_var = StringVar()
filter_var.trace('w', on_field_change)

combo_pl = ttk.Combobox(active, textvar = filter_var)  
combo_pl['values'] = all_player_list
#combo_pl.current(0)  # установите вариант по умолчанию  
combo_pl.grid(column=0, row=0, columnspan = 2)  

btn_add = Button(active, text="Добавить", command = add_player)  
btn_add.grid(column=0, row=1)  

btn_fill = Button(active, text="Удалить", command = remove_player)  
btn_fill.grid(column=1, row=1)  

players_list = Listbox(active)
players_list.grid(column=0, row=2, columnspan = 2, rowspan = 10)


empty = ["Свободно","не занимать"]

#window.geometry('600x450')  
combo1 = ttk.Combobox(window)  
combo1['values'] = empty
combo1.current(0)  # установите вариант по умолчанию  
combo1.grid(column=1, row=1)  

combo2 = ttk.Combobox(window)  
combo2['values'] = empty  
combo2.current(0)  # установите вариант по умолчанию  
combo2.grid(column=1, row=2)  

combo3 = ttk.Combobox(window)  
combo3['values'] = empty 
combo3.current(0)  # установите вариант по умолчанию  
combo3.grid(column=1, row=3)  

combo4 = ttk.Combobox(window)  
combo4['values'] = empty  
combo4.current(0)  # установите вариант по умолчанию  
combo4.grid(column=1, row=4)  

#change_places = ttk.Checkbutton(window, text = "пересадка",  command=set_uset_pos)  
#change_places.grid(column=1, row=5)



players_cb = [combo1, combo2, combo3, combo4]

for c in players_cb:
    c.bind('<<ComboboxSelected>>', on_select)



in1 = ttk.Entry(window)
in1.grid(column=2, row=1)
in2 = ttk.Entry(window)
in2.grid(column=2, row=2)
in3 = ttk.Entry(window)
in3.grid(column=2, row=3)
in4 = ttk.Entry(window)
in4.grid(column=2, row=4)

ins = [in1, in2, in3, in4]

btn_save = Button(window, text="Сохранить счет", command = save_match)  
btn_save.grid(column=3, row=1)  

btn_fill = Button(window, text="Заполнить", command=fullfill)  
btn_fill.grid(column=3, row=2)  

btn_clear = Button(window, text="Отчистить", command=clear_pl)  
btn_clear.grid(column=3, row=3)  

btn_predict = Button(window, text="Прогноз", command=predict)  
btn_predict.grid(column=3, row=4)  

full_log = Text(app, width = 150, wrap="none")
full_log.pack(side=LEFT)

write_log("Start")


app.mainloop()