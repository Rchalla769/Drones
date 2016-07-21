import cv2
import numpy as np

camera = cv2.VideoCapture(0)
while True:

    (grabbed, img) = camera.read()
    img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    ret, thresh1 = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)

    cv2.imshow('original', thresh1)

    if cv2.waitKey(1) == 27:
        exit(0)
