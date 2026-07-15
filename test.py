import RPi.GPIO as GPIO
import time
def vperednazad(i):
    
  ###down  

    for b in range(1, 100):
        GPIO.output(UD, GPIO.HIGH)
        GPIO.output(UD2, GPIO.LOW)
        print("ud high")
        GPIO.output(INC, GPIO.HIGH)
        print("inc high")
        GPIO.output(CS, GPIO.LOW)
        time.sleep(1/40)
        print("up")
        GPIO.output(INC, GPIO.LOW)
        print("inc LOW")
        time.sleep(1/40)
        GPIO.output(INC, GPIO.HIGH)
        time.sleep(1/40)
        GPIO.output(CS, GPIO.HIGH)
        
    for c in range(1, 100): 
        GPIO.output(UD, GPIO.LOW)
        GPIO.output(UD2, GPIO.HIGH)
        print("ud low")
        GPIO.output(INC, GPIO.HIGH)
        GPIO.output(CS, GPIO.LOW)
        time.sleep(1/40)
        print("down")
        GPIO.output(INC, GPIO.LOW)
        time.sleep(1/40)
        GPIO.output(INC, GPIO.HIGH)
        time.sleep(1/40)
        GPIO.output(CS, GPIO.HIGH)       
        
        
GPIO.setmode(GPIO.BCM)
CS = 26
INC = 20
UD = 21
UD2= 19


GPIO.setup(CS, GPIO.OUT)
GPIO.setup(INC, GPIO.OUT)
GPIO.setup(UD, GPIO.OUT)
GPIO.setup(UD2, GPIO.OUT)
GPIO.output(CS, GPIO.HIGH)
for i in range(1, 100):
    print(i)
    
     
    vperednazad(i)
GPIO.output(CS, GPIO.HIGH)
GPIO.cleanup()