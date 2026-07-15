#!/usr/bin/env python
import socket
import RPi.GPIO as GPIO
import time
from _thread import *
def client_thread(con):
    data = con.recv(1024)
    message = data.decode()
    datalist = message.split(" ")
    print(datalist)


    if datalist[0] == "start1gear0":
        start1gear0()
    elif datalist[0] == "start1gear1":
        start1gear1()
    elif datalist[0] == "start1gear2":
        start1gear2()
    elif datalist[0] == "start1gear3":
        start1gear3()
    elif datalist[0] == "start1gear4":
        start1gear4()
    elif datalist[0] == "start1gear5":
        start1gear5()
    elif datalist[0] == "start2gear0":
        start2gear0()
    elif datalist[0] == "start2gear1":
        start2gear1()
    elif datalist[0] == "start2gear2":
        start2gear2()
    elif datalist[0] == "start2gear3":
        start2gear3()
    elif datalist[0] == "start2gear4":
        start2gear4()
    elif datalist[0] == "start2gear5":
        start2gear5()
    elif datalist[0] == "revers1":
        revers1(int(datalist[1]))
    elif datalist[0] == "revers2":
        revers2(int(datalist[1])) 
    elif datalist[0] == "relle2pos":
        relle2pos(int(datalist[1]))
    elif datalist[0] == "relle1pos":
        relle1pos(int(datalist[1]))
    elif datalist[0] == "startUs1":
        startUs1(int(datalist[1]))
    elif datalist[0] == "startUs2":
        startUs2(int(datalist[1]))
    elif datalist[0] == "startUs3":
        startUs3(int(datalist[1]))
    elif datalist[0] == "startUs4":
        startUs4(int(datalist[1]))
    messageout = datalist[0][::-1]
    con.send(messageout.encode())
    con.close()
def startUs4(arg):
    if arg == 1:
        GPIO.output(GPIO07, GPIO.LOW)           
    elif arg == 0:
        GPIO.output(GPIO07, GPIO.HIGH)
def startUs3(arg):
    if arg == 1:
        GPIO.output(GPIO08, GPIO.LOW)           
    elif arg == 0:
        GPIO.output(GPIO08, GPIO.HIGH)
def startUs2(arg):
    if arg == 1:
        GPIO.output(GPIO15, GPIO.LOW)           
    elif arg == 0:
        GPIO.output(GPIO15, GPIO.HIGH) 
def startUs1(arg):
    if arg == 1:
        GPIO.output(GPIO10, GPIO.LOW)           
    elif arg == 0:
        GPIO.output(GPIO10, GPIO.HIGH)           

def revers1(arg):
    if arg == 1:
        GPIO.output(GPIO24, GPIO.HIGH)          
        GPIO.output(GPIO16, GPIO.HIGH)  
    elif arg == -1:
        GPIO.output(GPIO24, GPIO.LOW)           
        GPIO.output(GPIO16, GPIO.LOW)
def revers2(arg):
    if arg == 1:
        GPIO.output(GPIO17, GPIO.HIGH)          
        GPIO.output(GPIO19, GPIO.HIGH)  
    elif arg == -1:
        GPIO.output(GPIO17, GPIO.LOW)           
        GPIO.output(GPIO19, GPIO.LOW)
def start1gear0():
     GPIO.output(GPIO13, GPIO.HIGH)   ###relle 11   5KOM         
     GPIO.output(GPIO26, GPIO.HIGH)  ##relle 12   4.5 KOM
     GPIO.output(GPIO4, GPIO.HIGH)  ###relle 13   4KOM
     GPIO.output(GPIO25, GPIO.HIGH)  ###relle 14  3.3 KOM         
     GPIO.output(GPIO5, GPIO.HIGH)   ###relle 15       2 KOM        
     GPIO.output(GPIO27, GPIO.HIGH)    ###relle 16     0.1KOM        

def start2gear0():
     GPIO.output(GPIO21, GPIO.HIGH)   ###relle 6   5KOM         
     GPIO.output(GPIO23, GPIO.HIGH)  ##relle 5   4.5 KOM
     GPIO.output(GPIO18, GPIO.HIGH)  ###relle 4   4KOM
     GPIO.output(GPIO22, GPIO.HIGH)  ###relle 3  3.3 KOM         
     GPIO.output(GPIO6, GPIO.HIGH)   ###relle 2       2 KOM        
     GPIO.output(GPIO12, GPIO.HIGH)    ###relle 1 0.1KOM 



def start1gear1():
     GPIO.output(GPIO13, GPIO.LOW)   ###relle 11   5KOM         
     GPIO.output(GPIO26, GPIO.LOW)  ##relle 12   4.5 KOM
     GPIO.output(GPIO4, GPIO.HIGH)  ###relle 13   4KOM
     GPIO.output(GPIO25, GPIO.HIGH)  ###relle 14  3.3 KOM         
     GPIO.output(GPIO5, GPIO.HIGH)   ###relle 15       2 KOM        
     GPIO.output(GPIO27, GPIO.HIGH)    ###relle 16     0.1KOM
     
def start2gear1():
     GPIO.output(GPIO21, GPIO.LOW)   ###relle 6   5KOM         
     GPIO.output(GPIO23, GPIO.LOW)  ##relle 5   4.5 KOM
     GPIO.output(GPIO18, GPIO.HIGH)  ###relle 4   4KOM
     GPIO.output(GPIO22, GPIO.HIGH)  ###relle 3  3.3 KOM         
     GPIO.output(GPIO6, GPIO.HIGH)   ###relle 2       2 KOM        
     GPIO.output(GPIO12, GPIO.HIGH)    ###relle 1 0.1KOM      
     
def start1gear2():
     GPIO.output(GPIO13, GPIO.LOW)   ###relle 11   5KOM         
     GPIO.output(GPIO26, GPIO.HIGH)  ##relle 12   4.5 KOM
     GPIO.output(GPIO4, GPIO.LOW)  ###relle 13   4KOM
     GPIO.output(GPIO25, GPIO.HIGH)  ###relle 14  3.3 KOM         
     GPIO.output(GPIO5, GPIO.HIGH)   ###relle 15       2 KOM        
     GPIO.output(GPIO27, GPIO.HIGH)    ###relle 16     0.1KOM

def start2gear2():
     GPIO.output(GPIO21, GPIO.LOW)   ###relle 6   5KOM         
     GPIO.output(GPIO23, GPIO.HIGH)  ##relle 5   4.5 KOM
     GPIO.output(GPIO18, GPIO.LOW)  ###relle 4   4KOM
     GPIO.output(GPIO22, GPIO.HIGH)  ###relle 3  3.3 KOM         
     GPIO.output(GPIO6, GPIO.HIGH)   ###relle 2       2 KOM        
     GPIO.output(GPIO12, GPIO.HIGH)    ###relle 1 0.1KOM

def start1gear3():
     GPIO.output(GPIO13, GPIO.LOW)   ###relle 11   5KOM         
     GPIO.output(GPIO26, GPIO.HIGH)  ##relle 12   4.5 KOM
     GPIO.output(GPIO4, GPIO.HIGH)  ###relle 13   4KOM
     GPIO.output(GPIO25, GPIO.LOW)  ###relle 14  3.3 KOM         
     GPIO.output(GPIO5, GPIO.HIGH)   ###relle 15       2 KOM        
     GPIO.output(GPIO27, GPIO.HIGH)    ###relle 16     0.1KOM
     
def start2gear3():
     GPIO.output(GPIO21, GPIO.LOW)   ###relle 6   5KOM         
     GPIO.output(GPIO23, GPIO.HIGH)  ##relle 5   4.5 KOM
     GPIO.output(GPIO18, GPIO.HIGH)  ###relle 4   4KOM
     GPIO.output(GPIO22, GPIO.LOW)  ###relle 3  3.3 KOM         
     GPIO.output(GPIO6, GPIO.HIGH)   ###relle 2       2 KOM        
     GPIO.output(GPIO12, GPIO.HIGH)    ###relle 1 0.1KOM

def start2gear4():
     GPIO.output(GPIO21, GPIO.LOW)   ###relle 6   5KOM         
     GPIO.output(GPIO23, GPIO.HIGH)  ##relle 5   4.5 KOM
     GPIO.output(GPIO18, GPIO.HIGH)  ###relle 4   4KOM
     GPIO.output(GPIO22, GPIO.HIGH)  ###relle 3  3.3 KOM         
     GPIO.output(GPIO6, GPIO.LOW)   ###relle 2       2 KOM        
     GPIO.output(GPIO12, GPIO.HIGH)    ###relle 1 0.1KOM

def start1gear4():
     GPIO.output(GPIO13, GPIO.LOW)   ###relle 11   5KOM         
     GPIO.output(GPIO26, GPIO.HIGH)  ##relle 12   4.5 KOM
     GPIO.output(GPIO4, GPIO.HIGH)  ###relle 13   4KOM
     GPIO.output(GPIO25, GPIO.HIGH)  ###relle 14  3.3 KOM         
     GPIO.output(GPIO5, GPIO.LOW)   ###relle 15       2 KOM        
     GPIO.output(GPIO27, GPIO.HIGH)    ###relle 16     0.1KOM

def start2gear5():
     GPIO.output(GPIO21, GPIO.LOW)   ###relle 6   5KOM         
     GPIO.output(GPIO23, GPIO.HIGH)  ##relle 5   4.5 KOM
     GPIO.output(GPIO18, GPIO.HIGH)  ###relle 4   4KOM
     GPIO.output(GPIO22, GPIO.HIGH)  ###relle 3  3.3 KOM         
     GPIO.output(GPIO6, GPIO.HIGH)   ###relle 2       2 KOM        
     GPIO.output(GPIO12, GPIO.LOW)    ###relle 1 0.1KOM

def start1gear5():
     GPIO.output(GPIO13, GPIO.LOW)   ###relle 11   5KOM         
     GPIO.output(GPIO26, GPIO.HIGH)  ##relle 12   4.5 KOM
     GPIO.output(GPIO4, GPIO.HIGH)  ###relle 13   4KOM
     GPIO.output(GPIO25, GPIO.HIGH)  ###relle 14  3.3 KOM         
     GPIO.output(GPIO5, GPIO.HIGH)   ###relle 15       2 KOM        
     GPIO.output(GPIO27, GPIO.LOW)    ###relle 16     0.1KOM
GPIO4 = 4  ###relle 13   4KOM
GPIO17 =17  ###relle 8
GPIO27 =27  ###relle 16     0.1KOM
GPIO22 =22  ###relle 3
GPIO5 =5  ###relle 15       2 KOM
GPIO6 =6  ###relle 2
GPIO13 =13  ###relle 11  5KOM
GPIO19 =19  ###relle 7
GPIO26 =26  ###relle 12   4.5 KOM
GPIO18 =18  ###relle 4
GPIO23 =23  ###relle 5
GPIO24 =24  ###relle 10
GPIO25 =25  ###relle 14  3.3 KOM



def relle1pos(arg):
    if arg == 1:
          
        GPIO.output(GPIO3, GPIO.HIGH)
    elif arg == 0:
        GPIO.output(GPIO3, GPIO.LOW)
            

        
def relle2pos(arg):
    if arg == 1:
        
        GPIO.output(GPIO2, GPIO.LOW) #nizhniy blok relle relle 1
        GPIO.output(GPIO9, GPIO.LOW) #nizhniy blok relle relle  6
        GPIO.output(GPIO11, GPIO.HIGH) #nizhniy blok relle relle  3
        GPIO.output(GPIO14, GPIO.HIGH) #nizhniy blok relle relle 2 , 4 ,7
        
    elif arg == 0:
        GPIO.output(GPIO2, GPIO.HIGH) #nizhniy blok relle relle 1
        GPIO.output(GPIO9, GPIO.HIGH) #nizhniy blok relle relle  6
        GPIO.output(GPIO11, GPIO.HIGH) #nizhniy blok relle relle  3
        GPIO.output(GPIO14, GPIO.HIGH) #nizhniy blok relle relle 2 , 4 ,7
    elif arg == 2:
        GPIO.output(GPIO2, GPIO.HIGH) #nizhniy blok relle relle 1
        GPIO.output(GPIO9, GPIO.LOW) #nizhniy blok relle relle  6
        GPIO.output(GPIO11, GPIO.LOW) #nizhniy blok relle relle  3
        GPIO.output(GPIO14, GPIO.HIGH) #nizhniy blok relle relle 2 , 4 ,7
    elif arg == 3:
        GPIO.output(GPIO2, GPIO.LOW) #nizhniy blok relle relle 1
        GPIO.output(GPIO9, GPIO.LOW) #nizhniy blok relle relle  6
        GPIO.output(GPIO11, GPIO.HIGH) #nizhniy blok relle relle  3
        GPIO.output(GPIO14, GPIO.LOW) #nizhniy blok relle relle 2 , 4 ,7
    elif arg == 4:
        GPIO.output(GPIO2, GPIO.HIGH) #nizhniy blok relle relle 1
        GPIO.output(GPIO9, GPIO.LOW) #nizhniy blok relle relle  6
        GPIO.output(GPIO11, GPIO.LOW) #nizhniy blok relle relle  3
        GPIO.output(GPIO14, GPIO.LOW) #nizhniy blok relle relle 2 , 4 ,7

GPIO.setmode(GPIO.BCM)
GPIO4 = 4  ###relle 13   4KOM 1st
GPIO17 =17  ###relle 8 	revers2
GPIO27 =27  ###relle 16     0.1KOM 1st
GPIO22 =22  ###relle 3		3.3kom 2nd
GPIO5  = 5  ###relle 15       2 KOM  1st
GPIO6  = 6  ###relle 2	  2kom 2nd 
GPIO13 =13  ###relle 11  5KOM  1st
GPIO19 =19  ###relle 7	revers2
GPIO26 =26  ###relle 12   4.5 KOM 1st
GPIO18 =18  ###relle 4    4 kom 2nd
GPIO23 =23  ###relle 5  4.5 KOM
GPIO24 =24  ###relle 10 revers 1
GPIO25 =25  ###relle 14  3.3 KOM  1st
GPIO12 =12  ###relle 1  0.1 kom 2nd
GPIO16 =16  ###relle 9revers 1 
GPIO21 =21  ###relle 6  5kom 2nd
GPIO14 =14  ###relle 
GPIO3  =3  ###relle 
GPIO2  =2  ###relle
GPIO3  =3  ###relle 
GPIO9  =9  ###relle 3 nizhnie blok 
GPIO11 =11  ###relle 6 nizhnie blok 
GPIO10 =10  ### relle 10 niz
GPIO07 =7  ###relle 11 niz
GPIO08 =8  ###relle 12 niz
GPIO15 =15  ###relle 9 niz

GPIO.setup(GPIO10, GPIO.OUT)
GPIO.output(GPIO10, GPIO.HIGH)

GPIO.setup(GPIO07, GPIO.OUT)
GPIO.output(GPIO07, GPIO.HIGH)

GPIO.setup(GPIO08, GPIO.OUT)
GPIO.output(GPIO08, GPIO.HIGH)

GPIO.setup(GPIO15, GPIO.OUT)
GPIO.output(GPIO15, GPIO.HIGH)

GPIO.setup(GPIO4, GPIO.OUT)
GPIO.output(GPIO4, GPIO.HIGH)

GPIO.setup(GPIO17, GPIO.OUT)
GPIO.output(GPIO17, GPIO.HIGH)

GPIO.setup(GPIO27, GPIO.OUT)
GPIO.output(GPIO27, GPIO.HIGH)

GPIO.setup(GPIO22, GPIO.OUT)
GPIO.output(GPIO22, GPIO.HIGH)

GPIO.setup(GPIO5, GPIO.OUT)
GPIO.output(GPIO5, GPIO.HIGH)

GPIO.setup(GPIO6, GPIO.OUT)
GPIO.output(GPIO6, GPIO.HIGH)

GPIO.setup(GPIO13, GPIO.OUT)
GPIO.output(GPIO13, GPIO.HIGH)

GPIO.setup(GPIO19, GPIO.OUT)
GPIO.output(GPIO19, GPIO.HIGH)

GPIO.setup(GPIO26, GPIO.OUT)
GPIO.output(GPIO26, GPIO.HIGH)

GPIO.setup(GPIO18, GPIO.OUT)
GPIO.output(GPIO18, GPIO.HIGH)

GPIO.setup(GPIO23, GPIO.OUT)
GPIO.output(GPIO23, GPIO.HIGH)

GPIO.setup(GPIO24, GPIO.OUT)
GPIO.output(GPIO24, GPIO.HIGH)

GPIO.setup(GPIO25, GPIO.OUT)
GPIO.output(GPIO25, GPIO.HIGH)

GPIO.setup(GPIO12, GPIO.OUT)
GPIO.output(GPIO12, GPIO.HIGH)

GPIO.setup(GPIO16, GPIO.OUT)
GPIO.output(GPIO16, GPIO.HIGH)

GPIO.setup(GPIO21, GPIO.OUT)
GPIO.output(GPIO21, GPIO.HIGH)

GPIO.setup(GPIO14, GPIO.OUT)
GPIO.output(GPIO14, GPIO.HIGH) #

GPIO.setup(GPIO3, GPIO.OUT)
GPIO.output(GPIO3, GPIO.HIGH)

GPIO.setup(GPIO2, GPIO.OUT)
GPIO.output(GPIO2, GPIO.HIGH) #

GPIO.setup(GPIO9, GPIO.OUT)
GPIO.output(GPIO9, GPIO.HIGH) #

GPIO.setup(GPIO11, GPIO.OUT)
GPIO.output(GPIO11, GPIO.HIGH) #

server = socket.socket()
hostname = "192.168.8.4"
#hostname = "192.168.0.171" 
print(hostname)
port = 12345
server.bind((hostname, port))
server.listen(5)

print("servet run")
while True:
     
    client, _ = server.accept()
    start_new_thread(client_thread, (client, ))


