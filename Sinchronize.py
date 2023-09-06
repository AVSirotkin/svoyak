#Sinchronize
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import datetime as dt
from queue import Queue
import time


class my_sinch:

    all_players_info = []
    messages = Queue()
    run = True

    def init_connection(self):
        scopes = [
        'https://www.googleapis.com/auth/spreadsheets.readonly',
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
        ]

        credentials = ServiceAccountCredentials.from_json_keyfile_name("svoyak.json", scopes) #access the json key you downloaded earlier 
        file = gspread.authorize(credentials) # authenticate the JSON key with gspread
        # sheet = file.open("ЛогСвояка-Сезон2023-Весна") 
        sheet = file.open("Свояк Осень 2023") 

        players_sheet = sheet.worksheet("Рейтинг") 

        result_list_name = str(dt.datetime.today().strftime('%Y-%m-%d'))
        run = True

#tmp = sheeеt.duplicate(new_sheet_name="новая дата")
        today_sheet = 0
        total_players = 0

        for sh in sheet.worksheets():
            if sh.title == result_list_name:
                today_sheet = sh
                break

        if not today_sheet:
            today_sheet = sheet.worksheet("Бланки на 4").duplicate(new_sheet_name=result_list_name)
            today_sheet.update_cell(1, 4, result_list_name)
            #result_list_name записать

        self.all_players_info = players_sheet.get_all_records()
        self.today_sheet = today_sheet
    
    def update_cell(self, x, y, cont):
        self.messages.put((x, y, cont))

    def process_queue(self):
        while self.run:
            while not self.messages.empty():
                x,y,nfo = self.messages.get()
                self.today_sheet.update_cell(x,y,nfo)
            time.sleep(1)




